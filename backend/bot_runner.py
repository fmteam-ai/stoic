"""Autonomous bot runner — asyncio loop that drives every active user's bot.

Runs as a single asyncio task started at app startup. Each loop tick:
  1. Find all bot_configs with active=true
  2. For each user, run circuit-breaker check (drawdown kill switch)
  3. For each symbol on cooldown elapsed, generate a fresh dual-AI signal
  4. If signal.tradeable AND auto_execute AND has at least one connected account,
     queue a trade (status='pending') for the EA bridge to fulfil
  5. Broadcast every event to that user's WebSocket subscribers
"""
import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from bson import ObjectId

from database import get_db
from ai_signals import analyze_symbol  # noqa: F401  (kept for legacy callers/tests)
from agents.orchestrator import get_orchestrator
from circuit_breakers import check_and_trip
from ws_manager import manager as ws_manager
from rate_limiter import check_and_record as rl_check
from execution import for_account as engine_for_account
from execution import settle_paper_trades_against_price
from trigger_sweeper import sweep_once as sweep_triggers
from sl_watcher import sweep_once as sweep_sl_imminent
from position_protector import sweep_all as sweep_pre_news
from subscription_service import is_active as subscription_active
from auto_tune import get_auto_threshold
from intelligence_counters import increment as inc_intel_counter
from risk import get_profile, compute_lot_for_account
from portfolio.auto_deleverage import sweep as sweep_auto_deleverage
from portfolio.correlation_kelly import compute_correlation_aware_scale
from research_agent.self_improver import daily_sweep as sweep_research_agent

logger = logging.getLogger("bot-runner")

# Internal cooldown tracker { (user_id, symbol): datetime_next_eligible }
_next_signal_at: dict = {}


def _loop_interval() -> int:
    return int(os.environ.get("BOT_LOOP_INTERVAL_SEC", "60"))


def _cooldown_minutes(cfg: dict | None = None) -> int:
    """Minutes the bot waits between successive signals on the same symbol.

    Resolution order (highest priority first):
      1. `cfg.signal_cooldown_minutes` (per-bot override from UI / API).
      2. `BOT_SIGNAL_COOLDOWN_MIN` env var.
      3. Default 5.
    Clamped to the loop interval (60s) — values below 1 minute are
    pointless because the loop only ticks once per 60s.
    """
    val = None
    if cfg is not None:
        v = cfg.get("signal_cooldown_minutes")
        if v is not None:
            try:
                val = int(v)
            except (TypeError, ValueError):
                val = None
    if val is None:
        val = int(os.environ.get("BOT_SIGNAL_COOLDOWN_MIN", "5"))
    return max(1, val)


def _sl_cooldown_minutes_default() -> int:
    return int(os.environ.get("SL_COOLDOWN_MIN", "45"))


async def _on_sl_cooldown(db, user_id: str, symbol: str, lookback_min: int) -> dict | None:
    """If the most recent trade for (user, symbol) was a stop-out within
    `lookback_min` minutes, return that trade's metadata. Used to suppress
    revenge re-entries into the same losing regime.
    """
    if lookback_min <= 0:
        return None
    cursor = db.trades.find({
        "user_id": user_id,
        "symbol": symbol,
        "status": "closed",
        "close_reason": "stop_loss",
    }).sort("closed_at", -1).limit(1)
    docs = await cursor.to_list(length=1)
    if not docs:
        return None
    last = docs[0]
    closed_at = last.get("closed_at")
    if not closed_at:
        return None
    try:
        ts = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
    except Exception:
        return None
    age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
    if age_min < lookback_min:
        return {
            "trade_id": str(last.get("_id")),
            "age_minutes": round(age_min, 1),
            "lookback_minutes": lookback_min,
            "resumes_in_min": round(lookback_min - age_min, 1),
        }
    return None


def _on_cooldown(user_id: str, symbol: str) -> bool:
    next_at = _next_signal_at.get((user_id, symbol))
    return bool(next_at) and datetime.now(timezone.utc) < next_at


def _mark_cooldown(user_id: str, symbol: str, cfg: dict | None = None):
    _next_signal_at[(user_id, symbol)] = (
        datetime.now(timezone.utc) + timedelta(minutes=_cooldown_minutes(cfg))
    )


async def _record_pulse(
    db, cfg: dict, *,
    action: str, reason: str, level: str = "info",
    symbol: str | None = None, next_eligible_at=None,
):
    """Persist the latest bot-cycle verdict on the cfg doc.

    This is what surfaces on the Trades page so the user can see *why*
    an enabled bot is silent. We overwrite (no history) — the page polls
    /api/bot/pulse to render the current state.

    Args:
        action: HOLD | BUY | SELL | SKIP | EXEC | BLOCKED
        level:  info | warn | block — drives UI color (grey/amber/red)
    """
    try:
        nxt_iso = None
        if next_eligible_at:
            nxt_iso = (next_eligible_at.isoformat()
                       if hasattr(next_eligible_at, "isoformat") else str(next_eligible_at))
        pulse = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "action": action,
            "reason": reason,
            "level": level,
            "next_eligible_at": nxt_iso,
        }
        await db.bot_configs.update_one(
            {"_id": cfg["_id"]},
            {"$set": {"_last_pulse": pulse}},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to persist pulse cfg=%s: %s", cfg.get("_id"), e)


async def _connected_accounts(db, user_id: str) -> list:
    """Return list of accounts ready to accept trades.

    - PAPER accounts are always "connected" (virtual)
    - LIVE accounts must have an EA heartbeat in the last 5 minutes
    """
    cursor = db.accounts.find({"user_id": user_id})
    accs = await cursor.to_list(length=50)
    fresh = []
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    for a in accs:
        if (a.get("mode") or "live") == "paper":
            fresh.append(a)
            continue
        hb = a.get("last_heartbeat") or ""
        if hb and hb >= cutoff:
            fresh.append(a)
    return fresh


async def _process_user_account(db, cfg: dict):
    """Run one bot config's tick — scoped to either the user's default profile
    (one trade per signal, on first connected account) or a specific account
    (independent per-account bot with its own settings/lot cap/symbols)."""
    cfg_account_id = cfg.get("account_id")  # None = default profile
    symbols = cfg.get("symbols") or []
    if not symbols:
        return

    # === Atomic config lock — prevents concurrent bot_runner.loop() instances
    # (from uvicorn hot reloads or duplicate startup tasks) racing each other
    # and blowing past max_concurrent_trades. findOneAndUpdate atomically
    # claims a 50-second lease on this cfg. If another loop already holds it,
    # skip this tick entirely.
    now_utc = datetime.now(timezone.utc)
    lock_expiry = (now_utc + timedelta(seconds=50)).isoformat()
    lock_filter = {
        "_id": cfg["_id"],
        "$or": [
            {"_tick_lock_until": {"$exists": False}},
            {"_tick_lock_until": None},
            {"_tick_lock_until": {"$lt": now_utc.isoformat()}},
        ],
    }
    locked = await db.bot_configs.find_one_and_update(
        lock_filter, {"$set": {"_tick_lock_until": lock_expiry}},
    )
    if not locked:
        logger.debug("Skipping cfg=%s acct=%s — tick lock held by another runner",
                     str(cfg.get("_id")), cfg_account_id or "default")
        return

    try:
        await _process_user_account_locked(db, cfg)
    finally:
        # Release the tick lock so the next loop iteration isn't starved for 50s
        # if signal-generation crashed mid-flight. Setting to None lets the next
        # findOneAndUpdate claim the lock immediately.
        await db.bot_configs.update_one(
            {"_id": cfg["_id"]}, {"$set": {"_tick_lock_until": None}},
        )


async def _process_user_account_locked(db, cfg: dict):
    """The actual per-cfg tick logic, executed under the atomic lock."""
    user_id = cfg["user_id"]
    cfg_account_id = cfg.get("account_id")
    symbols = cfg.get("symbols") or []

    # 0. Subscription gate — paper accounts always allowed; live execution requires active sub
    entitlement = await subscription_active(user_id)

    # Resolve which accounts this cfg controls.
    #   - Per-account cfg: only that single account.
    #   - Default cfg: every account that does NOT have its own per-account cfg.
    if cfg_account_id:
        all_accounts = await db.accounts.find(
            {"user_id": user_id, "_id": ObjectId(cfg_account_id)}
        ).to_list(length=1)
    else:
        all_accounts = await db.accounts.find({"user_id": user_id}).to_list(length=50)
        if all_accounts:
            # Filter out accounts that have their OWN active/inactive override config —
            # those are managed by their dedicated cfg pass (independent bot).
            overridden = await db.bot_configs.find({
                "user_id": user_id,
                "account_id": {"$nin": [None]},
            }, {"account_id": 1}).to_list(length=50)
            ov_ids = {o["account_id"] for o in overridden if o.get("account_id")}
            all_accounts = [a for a in all_accounts if str(a["_id"]) not in ov_ids]

    if not all_accounts:
        await _record_pulse(db, cfg,
            action="BLOCKED", level="block",
            reason="No accounts configured for this bot — connect an MT5 or Binance account.",
        )
        return

    # 1. Circuit breaker check first — never analyse if tripped
    cb = await check_and_trip(db, user_id, cfg, all_accounts)
    if cb["tripped"]:
        await ws_manager.broadcast(user_id, "circuit_breaker_tripped", cb)
        logger.warning("Circuit breaker tripped for user=%s acct=%s: %s",
                       user_id, cfg_account_id or "default", cb["reason"])
        await _record_pulse(db, cfg,
            action="BLOCKED", level="block",
            reason=f"Circuit breaker tripped — {cb.get('reason', 'drawdown limit hit')}",
        )
        return

    # 1b. Daily profit target check — upside mirror of the circuit breaker.
    # If hit + mode=stop → bot disables itself for the day.
    # If hit + mode=lock → persist lock state (sizing reads it later).
    from profit_target import apply_profit_target
    pt = await apply_profit_target(db, user_id, cfg, all_accounts)
    if pt and pt.get("hit"):
        await ws_manager.broadcast(user_id, "daily_profit_target_hit", pt)
        if pt["should_stop"]:
            logger.info("Profit target STOP for user=%s acct=%s: +$%.2f ≥ %sR",
                        user_id, cfg_account_id or "default",
                        pt["current_pnl"], pt["target_r"])
            await _record_pulse(db, cfg,
                action="BLOCKED", level="block",
                reason=(f"Daily profit target hit: +${pt['current_pnl']:.2f} "
                        f"≥ {pt['target_r']}R — bot paused until 00:00 UTC."),
            )
            return
        # lock mode → refresh in-memory cfg so the rest of this cycle sees it
        from profit_target import locked_profit_amount  # noqa: F401  (used downstream)
        # Re-read the freshly-updated cfg so sizing picks up the new lock.
        cfg_fresh = await db.bot_configs.find_one({"_id": cfg["_id"]})
        if cfg_fresh:
            cfg["_profit_lock"] = cfg_fresh.get("_profit_lock")

    # Paper-mode-only fallback when subscription is inactive
    if not entitlement["active"]:
        all_accounts = [a for a in all_accounts if (a.get("mode") or "live") == "paper"]
        if not all_accounts:
            await _record_pulse(db, cfg,
                action="BLOCKED", level="block",
                reason="Subscription inactive — add a paper account or renew to resume.",
            )
            return

    connected = [a for a in all_accounts
                 if (a.get("mode") or "live") == "paper"
                 or (a.get("last_heartbeat") and
                     a["last_heartbeat"] >= (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())]

    # iter-71 · Broker-rejection circuit breaker — skip accounts the broker
    # keeps rejecting orders on (e.g. VT Markets with symbol-suffix mismatch
    # returning retcode 10013 repeatedly). Stops the bleeding until the user
    # fixes the underlying issue and explicitly unblocks the account.
    from broker_reject_breaker import evaluate_account as _eval_broker_reject
    survivors: list = []
    blocked_summaries: list = []
    for a in connected:
        verdict = await _eval_broker_reject(db, a)
        if verdict["blocked"]:
            blocked_summaries.append({
                "label": a.get("label"),
                "retcode": verdict.get("retcode"),
                "retcode_label": verdict.get("label"),
                "hint": verdict.get("hint"),
            })
            if verdict["tripped_this_call"]:
                await ws_manager.broadcast(user_id, "account_trading_blocked", {
                    "account_id": str(a["_id"]),
                    "label": a.get("label"),
                    "retcode": verdict["retcode"],
                    "label_human": verdict["label"],
                    "hint": verdict["hint"],
                    "reason": verdict["block_reason"],
                })
                logger.warning("Auto-halted account=%s user=%s — %s",
                               a.get("label"), user_id, verdict["block_reason"])
                # iter-72 · Push Telegram alert (fire-and-forget).
                try:
                    from notifier import notify_account_blocked
                    await notify_account_blocked(
                        user_id, str(a["_id"]),
                        verdict["retcode"] or "?",
                        verdict["label"] or "broker rejection",
                        verdict["hint"] or "",
                    )
                except Exception:
                    pass
            await _record_pulse(db, cfg,
                action="BLOCKED", level="block",
                reason=f"Account {a.get('label')} halted: {verdict['block_reason']}",
            )
            continue
        survivors.append(a)
    connected = survivors
    # Cached on cfg for the inner loop's "no connected accounts" branch so the
    # pulse there can show "broker auto-halted" instead of the generic
    # "EA heartbeat stale" message.
    cfg["_blocked_account_summaries"] = blocked_summaries

    # iter-71b · If every account on this cfg is auto-halted by the broker
    # breaker, record the precise reason at cfg-level NOW (before any
    # per-symbol cooldown short-circuits) so the dashboard pulse shows the
    # actionable error instead of a misleading "cooldown active" or
    # "EA heartbeat stale".
    if not connected and blocked_summaries:
        bs = blocked_summaries[0]
        await _record_pulse(db, cfg,
            action="BLOCKED", level="block",
            reason=(
                f"All accounts auto-halted by broker: "
                f"{bs.get('retcode_label') or 'broker rejection'} "
                f"({bs.get('retcode') or '?'}) on {bs.get('label')}. "
                f"{bs.get('hint') or 'Open Accounts → Resume Trading once fixed.'}"
            ),
        )
        return

    risk_level = cfg.get("risk_level", "medium")
    auto_exec = bool(cfg.get("auto_execute", True))
    # Paper-shadow mode (iter-39): when the cfg is in shadow mode but not
    # fully active, run the entire signal pipeline but FORCE auto_execute
    # off. Signals get tagged origin='shadow' so the UI can filter them
    # out of "real" performance reports.
    shadow_only = bool(cfg.get("paper_shadow_mode")) and not bool(cfg.get("active"))
    if shadow_only:
        auto_exec = False
    max_concurrent = int(cfg.get("max_concurrent_trades", 3))
    max_lot_cap = float(cfg.get("max_lot_size") or 0.0)
    auto_tune_enabled = bool(cfg.get("auto_tune_enabled", True))
    min_conf_override = int(cfg.get("min_confidence_override") or 0)
    aggressive_mode_cfg = bool(cfg.get("aggressive_mode") or False)
    spread_filter_enabled = bool(cfg.get("spread_filter_enabled", False))
    max_spread_pips = cfg.get("max_spread_pips") or {}
    anti_tilt_enabled = bool(cfg.get("anti_tilt_enabled", True))
    anti_tilt_n = int(cfg.get("anti_tilt_consecutive_losses", 3))
    anti_tilt_hours = int(cfg.get("anti_tilt_freeze_hours", 4))
    trade_of_day_cap = int(cfg.get("trade_of_day_cap", 1) or 0)
    asia_skip_xau = bool(cfg.get("asia_session_skip_xau", True))
    sl_cooldown_enabled = bool(cfg.get("sl_cooldown_enabled", True))
    sl_cooldown_min = int(cfg.get("sl_cooldown_minutes", _sl_cooldown_minutes_default()) or 0)

    # Cooldown / counters are scoped per (user, account) so multiple
    # per-account bots can fire concurrently on the same symbol.
    cooldown_scope = f"{user_id}:{cfg_account_id or 'default'}"

    # === Anti-tilt: freeze auto-execute if last N closed trades all lost ===
    anti_tilt_active = False
    if anti_tilt_enabled and anti_tilt_n > 0:
        recent_q = {"user_id": user_id, "status": "closed"}
        if cfg_account_id:
            recent_q["account_id"] = cfg_account_id
        recent = await db.trades.find(recent_q).sort(
            "closed_at", -1
        ).limit(anti_tilt_n).to_list(length=anti_tilt_n)
        if len(recent) == anti_tilt_n and all(float(r.get("pnl") or 0) <= 0 for r in recent):
            last_close = recent[0].get("closed_at")
            try:
                lc = datetime.fromisoformat(str(last_close).replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - lc < timedelta(hours=anti_tilt_hours):
                    anti_tilt_active = True
                    logger.warning("Anti-tilt active user=%s acct=%s — last %d trades lost",
                                   user_id, cfg_account_id or "default", anti_tilt_n)
            except Exception:
                pass

    # Count current open + pending trades to respect max_concurrent (per-account when scoped)
    inflight_q = {"user_id": user_id, "status": {"$in": ["pending", "open"]}}
    if cfg_account_id:
        inflight_q["account_id"] = cfg_account_id
    inflight = await db.trades.count_documents(inflight_q)

    if anti_tilt_active:
        await _record_pulse(db, cfg,
            action="BLOCKED", level="warn",
            reason=f"Anti-tilt freeze — last {anti_tilt_n} trades lost. Resumes in <{anti_tilt_hours}h.",
        )
        return

    for sym in symbols:
        if _on_cooldown(cooldown_scope, sym):
            await _record_pulse(db, cfg, symbol=sym,
                action="SKIP", level="info",
                reason=f"Signal cooldown active for {sym} ({_cooldown_minutes(cfg)}min between signals).",
                next_eligible_at=_next_signal_at.get((cooldown_scope, sym)),
            )
            continue

        # Capital-preservation guards (skip BEFORE expensive AI analysis)
        if asia_skip_xau and sym.upper() == "XAUUSD":
            now_h = datetime.now(timezone.utc).hour
            if now_h < 7:
                logger.info("Asia-session skip user=%s sym=%s hour=%d", user_id, sym, now_h)
                await _record_pulse(db, cfg, symbol=sym,
                    action="SKIP", level="info",
                    reason="Asia-session skip — XAUUSD pauses until 07:00 UTC (low liquidity).",
                )
                continue

        if trade_of_day_cap > 0:
            day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            tod_q = {
                "user_id": user_id,
                "symbol": sym,
                "created_at": {"$gte": day_start.isoformat()},
            }
            if cfg_account_id:
                tod_q["account_id"] = cfg_account_id
            today_count = await db.trades.count_documents(tod_q)
            if today_count >= trade_of_day_cap:
                logger.info("Trade-of-day cap reached user=%s sym=%s count=%d/%d",
                            user_id, sym, today_count, trade_of_day_cap)
                await _record_pulse(db, cfg, symbol=sym,
                    action="SKIP", level="info",
                    reason=f"Daily trade cap reached ({today_count}/{trade_of_day_cap}) for {sym}. Resets at 00:00 UTC.",
                )
                continue

        if sl_cooldown_enabled and sl_cooldown_min > 0:
            sl_cd = await _on_sl_cooldown(db, user_id, sym, sl_cooldown_min)
            if sl_cd:
                logger.info("SL cooldown user=%s sym=%s resumes in %smin",
                            user_id, sym, sl_cd["resumes_in_min"])
                await inc_intel_counter(user_id, "sl_cooldown_block")
                await _record_pulse(db, cfg, symbol=sym,
                    action="SKIP", level="info",
                    reason=f"Stop-loss cooldown — {sym} resumes in {sl_cd['resumes_in_min']}min (avoiding revenge re-entry).",
                )
                continue

        try:
            orch = get_orchestrator()
            active_positions_q = {
                "user_id": user_id,
                "status": {"$in": ["open", "pending"]},
            }
            if cfg_account_id:
                active_positions_q["account_id"] = cfg_account_id
            active_positions = await db.trades.find(active_positions_q).to_list(length=50)
            tick_out = await orch.analyze_tick(
                user_id=user_id, symbol=sym, risk_level=risk_level,
                active_positions=active_positions,
                user_cfg=cfg,
            )
            signal = tick_out.get("signal")
            if not signal:
                await _record_pulse(db, cfg, symbol=sym,
                    action="HOLD", level="warn",
                    reason="Analyser pipeline returned no signal (Strategy or Risk agent failed). Check logs.",
                )
                continue
        except Exception as e:
            logger.exception("orchestrator.analyze_tick failed user=%s sym=%s: %s", user_id, sym, e)
            await _record_pulse(db, cfg, symbol=sym,
                action="HOLD", level="warn",
                reason=f"Analyser exception: {type(e).__name__}",
            )
            continue

        # Auto-Tune
        auto_tune_block_reason = None
        if auto_tune_enabled:
            try:
                tune = await get_auto_threshold(user_id, sym, risk_level)
                signal["auto_tune"] = tune
                eff = float(tune.get("effective_threshold") or 0)
                if aggressive_mode_cfg or (0 < min_conf_override < 100):
                    cap = min_conf_override if (0 < min_conf_override < 100) else 100
                    eff = min(eff, float(cap))
                if (signal.get("confidence") or 0) < eff:
                    auto_tune_block_reason = (
                        f"Auto-tune raised threshold to {eff:.0f}% "
                        f"(source={tune.get('source')}, samples={tune.get('total_samples')}); "
                        f"signal {signal.get('confidence')}% — auto-execute skipped."
                    )
            except Exception as e:
                logger.exception("auto_tune failed user=%s sym=%s: %s", user_id, sym, e)

        try:
            mtf_gate = signal.get("mtf_gate") or {}
            if mtf_gate.get("checked") and mtf_gate.get("aligned") is False:
                await inc_intel_counter(user_id, "mtf_veto")
            lm = signal.get("learned_meta") or {}
            if lm.get("verdict") == "REJECT":
                await inc_intel_counter(user_id, "learned_meta_veto")
            aplus = signal.get("aplus_confluence") or {}
            if aplus.get("checks") and aplus.get("passed") is False:
                await inc_intel_counter(user_id, "aplus_veto")
            reasoning_blob = signal.get("reasoning") or ""
            if "VETO (R:R)" in reasoning_blob:
                await inc_intel_counter(user_id, "rr_veto")
        except Exception:
            pass

        # iter-73 · STRICT MTF mode (cfg.mtf_strict=True). On top of the
        # existing veto (which fires when ≥2/3 tiers disagree), this requires
        # ≥2/3 tiers to ACTIVELY AGREE with the trade direction. Counter-trend
        # AND drift-into-chop trades are both blocked.
        if signal.get("action") in ("BUY", "SELL") and bool(cfg.get("mtf_strict")):
            tiers = signal.get("mtf_tiers") or {}
            alignment = (tiers.get("alignment") or {})
            buy_sup = int(alignment.get("buy_support") or 0)
            sell_sup = int(alignment.get("sell_support") or 0)
            need = 2  # minimum tiers that must back the direction
            ok = (signal["action"] == "BUY" and buy_sup >= need) or \
                 (signal["action"] == "SELL" and sell_sup >= need)
            if not ok:
                await _record_pulse(db, cfg, symbol=sym,
                    action="SKIP", level="warn",
                    reason=(f"MTF strict mode: {signal['action']} needs ≥{need}/3 "
                            f"tiers agreeing — got buy={buy_sup} sell={sell_sup}. "
                            f"Trade skipped."),
                )
                await inc_intel_counter(user_id, "mtf_strict_veto")
                continue

        signal["user_id"] = user_id
        if cfg_account_id:
            signal["account_id"] = cfg_account_id  # signal tagged so UI can filter
        signal["consumed"] = False
        signal["created_at"] = datetime.now(timezone.utc).isoformat()
        signal["origin"] = "shadow" if shadow_only else "auto"
        result = await db.signals.insert_one(signal)
        signal_id = str(result.inserted_id)
        broadcast_payload = {**signal, "id": signal_id}
        broadcast_payload.pop("_id", None)
        await ws_manager.broadcast(user_id, "signal_created", broadcast_payload)
        _mark_cooldown(cooldown_scope, sym, cfg)

        try:
            if signal.get("action") in ("BUY", "SELL") and (signal.get("confidence") or 0) >= 75:
                from notifier import notify_high_conf_signal
                await notify_high_conf_signal(user_id, broadcast_payload)
        except Exception:
            pass

        if not (auto_exec and signal["tradeable"]):
            sig_act = signal.get("action") or "HOLD"
            sig_conf = signal.get("confidence") or 0
            if shadow_only:
                p_reason = f"Paper-shadow mode: signal {sig_act} {sig_conf}% logged (no live execution)."
                p_level = "info"
            elif not auto_exec:
                p_reason = f"Auto-execute disabled on this bot — signal {sig_act} {sig_conf}% not fired."
                p_level = "info"
            else:
                # Not tradeable — surface the AI's stated reasoning if present
                reason_blob = (signal.get("reasoning") or "")[:140].strip()
                p_reason = (f"AI {sig_act} {sig_conf}% — {reason_blob}"
                            if reason_blob else f"AI {sig_act} {sig_conf}% — signal not tradeable.")
                p_level = "info"
            await _record_pulse(db, cfg, symbol=sym,
                action=sig_act, level=p_level, reason=p_reason,
            )
            continue
        if auto_tune_block_reason:
            logger.info("Auto-tune block user=%s sym=%s: %s", user_id, sym, auto_tune_block_reason)
            await inc_intel_counter(user_id, "auto_tune_block")
            await _record_pulse(db, cfg, symbol=sym,
                action="SKIP", level="info", reason=auto_tune_block_reason,
            )
            continue
        if inflight >= max_concurrent:
            logger.info("Max concurrent (%s) reached user=%s acct=%s",
                        max_concurrent, user_id, cfg_account_id or "default")
            await _record_pulse(db, cfg, symbol=sym,
                action="SKIP", level="warn",
                reason=f"Max concurrent trades reached ({inflight}/{max_concurrent}) — close one to fire next signal.",
            )
            continue

        # Anti-pyramid (P0, iter-48) — refuse a second trade in the SAME
        # direction on the SAME symbol while a prior is still open.
        # On 2026-06-26 the bot fired 5 losing SELLs in 22min stacking ever
        # deeper into a rallying gold market because nothing prevented same-
        # direction pyramiding. Per-account scope to mirror inflight counting.
        pyramid_q = {
            "user_id": user_id,
            "symbol": sym,
            "action": signal["action"],
            "status": {"$in": ["pending", "open"]},
        }
        if cfg_account_id:
            pyramid_q["account_id"] = cfg_account_id
        same_dir_open = await db.trades.count_documents(pyramid_q)
        if same_dir_open > 0:
            await _record_pulse(db, cfg, symbol=sym,
                action="SKIP", level="warn",
                reason=(f"Anti-pyramid: {same_dir_open} {signal['action']} {sym} "
                        f"already open — refusing to stack same-direction risk."),
            )
            continue

        # Same-direction loss circuit-breaker (P0, iter-48) — if the LAST
        # `LOSS_STREAK_THRESHOLD` closed trades on this (symbol, action) all
        # lost, pause same-direction entries for `LOSS_STREAK_COOLDOWN_HRS`
        # hours. Stops the bot from doubling down into a clearly-wrong bias.
        LOSS_STREAK_THRESHOLD = 2
        LOSS_STREAK_COOLDOWN_HRS = 4
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=LOSS_STREAK_COOLDOWN_HRS)).isoformat()
        recent_closed = await db.trades.find(
            {
                "user_id": user_id, "symbol": sym, "action": signal["action"],
                "status": "closed", "closed_at": {"$gte": cutoff},
            },
        ).sort("closed_at", -1).limit(LOSS_STREAK_THRESHOLD).to_list(LOSS_STREAK_THRESHOLD)
        if (len(recent_closed) >= LOSS_STREAK_THRESHOLD
                and all(float(t.get("pnl") or 0) < 0 for t in recent_closed)):
            await _record_pulse(db, cfg, symbol=sym,
                action="SKIP", level="warn",
                reason=(f"Loss-streak circuit-breaker: last {LOSS_STREAK_THRESHOLD} "
                        f"{signal['action']} {sym} trades lost. "
                        f"Pausing same-direction entries for {LOSS_STREAK_COOLDOWN_HRS}h "
                        f"to break the bias trap."),
            )
            continue

        if not connected:
            blocked_summaries = cfg.get("_blocked_account_summaries") or []
            if blocked_summaries:
                # All live accounts on this cfg were auto-halted by the broker
                # circuit breaker. Surface the FIRST account's reason — it's
                # the actionable one for the user.
                bs = blocked_summaries[0]
                pulse_reason = (
                    f"All accounts auto-halted by broker: "
                    f"{bs.get('retcode_label') or 'broker rejection'} "
                    f"({bs.get('retcode') or '?'}) on {bs.get('label')}. "
                    f"{bs.get('hint') or 'See Accounts page → Resume Trading after fixing.'}"
                )
            else:
                pulse_reason = (
                    "No connected accounts — EA heartbeat stale (>5min). "
                    "Check MT5 EA / network."
                )
            await _record_pulse(db, cfg, symbol=sym,
                action="BLOCKED", level="block",
                reason=pulse_reason,
            )
            continue
        rl = rl_check(user_id)
        if not rl["allowed"]:
            logger.warning("Rate-limited user=%s, count_60s=%s/%s, skip auto-execute",
                           user_id, rl["count_60s"], rl["limit"])
            await _record_pulse(db, cfg, symbol=sym,
                action="SKIP", level="warn",
                reason=f"Rate-limited ({rl['count_60s']}/{rl['limit']} per 60s) — execution paused briefly.",
            )
            continue

        # Target account: per-account cfg pins one; default cfg picks the first connected.
        target_account = connected[0]

        if spread_filter_enabled and (target_account.get("mode") or "live") == "live":
            spreads = target_account.get("current_spreads") or {}
            current_sp = spreads.get(sym)
            cap = max_spread_pips.get(sym)
            if current_sp is not None and cap is not None and current_sp > float(cap):
                logger.info("Spread filter block user=%s sym=%s spread=%.1fp cap=%.1fp",
                            user_id, sym, current_sp, cap)
                await db.signals.update_one(
                    {"_id": result.inserted_id},
                    {"$set": {"spread_filter_block": {
                        "spread_pips": current_sp,
                        "cap_pips": cap,
                        "account_id": str(target_account["_id"]),
                    }}},
                )
                await inc_intel_counter(user_id, "spread_block")
                await _record_pulse(db, cfg, symbol=sym,
                    action="SKIP", level="info",
                    reason=f"Spread filter block — {sym} spread {current_sp:.1f}p > cap {cap:.1f}p.",
                )
                continue

        # Recompute lot size against the TARGET account's real equity and the
        # symbol's proper pip-value. The signal-time lot is computed with a
        # hardcoded $1000 equity (account-agnostic), which always over-shoots
        # on live accounts and made the user's max_lot_size cap permanently
        # binding. Doing it here ties sizing to actual risk management.
        profile = get_profile(risk_level)
        # Iter-65: pass today's locked daily-profit so it's removed from the
        # equity pool used for Kelly sizing. The locked $ becomes untouchable.
        from profit_target import locked_profit_amount
        locked = locked_profit_amount(cfg)
        sized = compute_lot_for_account(
            account=target_account,
            symbol=signal["symbol"],
            entry_price=signal["entry_price"],
            stop_loss=signal["stop_loss"],
            confidence_pct=float(signal.get("confidence") or 0),
            profile=profile,
            locked_profit=locked,
        )
        kelly_f = float(sized.get("kelly_f") or 0)
        kelly_cap = float(profile.get("kelly_cap") or 0)
        absolute_lot = float(sized["lot_size"])

        # Position-sizing strategy when user has set a `max_lot_size`:
        #   • Treat max_lot_size as the lot at PEAK Kelly (max confidence).
        #   • Scale linearly to lower confidence: lot = max × (kelly_f / kelly_cap).
        # This is what the user means by "use lots according to risk management
        # and not the maximum on every trade" — low-confidence signals get
        # proportionally smaller lots, high-confidence signals approach the cap.
        # When max_lot_size is 0 (unset), fall back to absolute Kelly sizing.
        if max_lot_cap > 0 and kelly_cap > 0:
            conf_scale = min(kelly_f / kelly_cap, 1.0) if kelly_f > 0 else 0.0
            scaled_lot = max(round(max_lot_cap * conf_scale, 2), 0.01)
            # Pick the smaller of: absolute Kelly lot vs confidence-scaled cap.
            effective_lot = min(absolute_lot, scaled_lot)
            sizing_method = "max_cap_kelly_scaled"
        else:
            effective_lot = absolute_lot
            # Legacy hard-ceiling clamp (no scaling — only when kelly_cap=0)
            if max_lot_cap > 0 and effective_lot > max_lot_cap:
                effective_lot = max_lot_cap
            sizing_method = "absolute_kelly"

        logger.info(
            "Lot sized acct=%s sym=%s equity=$%s conf=%s%% kelly_f=%s "
            "sl_pips=%s pip_usd=$%s abs_lot=%s max_cap=%s → lots=%s (method=%s)",
            cfg_account_id or "default", sym,
            sized.get("equity"), signal.get("confidence"),
            kelly_f, sized.get("sl_pips"), sized.get("pip_usd_per_lot"),
            absolute_lot, max_lot_cap, effective_lot, sizing_method,
        )

        # iter-51 · Correlation-aware portfolio allocation + dynamic CVaR
        # risk budget. Defence-in-depth lot trim AFTER Kelly + max-lot cap.
        # Disabled-by-default behind env flag so the wire-up is opt-in.
        corr_kelly_scale = 1.0
        corr_kelly_info: dict | None = None
        if os.environ.get("CORRELATION_KELLY_ENABLED", "true").lower() == "true":
            try:
                open_q = {"user_id": user_id, "status": {"$in": ["open", "pending"]}}
                if cfg_account_id:
                    open_q["account_id"] = cfg_account_id
                open_book = await db.trades.find(open_q).to_list(length=50)
                # Notional = lot × entry × (100 if XAU else 1) — matches var.py
                hypothetical_notional = (
                    effective_lot * float(signal["entry_price"])
                    * (100 if sym == "XAUUSD" else 1)
                )
                ck = await compute_correlation_aware_scale(
                    new_symbol=sym,
                    new_action=signal["action"],
                    new_notional=hypothetical_notional,
                    open_positions=open_book,
                    equity=float(target_account.get("equity") or target_account.get("balance") or 0),
                )
                corr_kelly_scale = float(ck.get("scale") or 1.0)
                corr_kelly_info = ck
                if corr_kelly_scale < 1.0:
                    trimmed = max(round(effective_lot * corr_kelly_scale, 2), 0.01)
                    logger.info(
                        "Correlation-Kelly trim acct=%s sym=%s lot=%s × scale=%.3f → %s · %s",
                        cfg_account_id or "default", sym, effective_lot,
                        corr_kelly_scale, trimmed, ck.get("reason"),
                    )
                    effective_lot = trimmed
                    sizing_method = sizing_method + "+corr_kelly"
            except Exception as e:  # noqa: BLE001
                logger.debug("Correlation-Kelly skipped (%s) — proceeding without trim", e)

        # iter-58 · Pre-trade sector-cap fit. Prevents the bot from opening
        # trades that would immediately breach the per-account sector cap
        # (e.g., XAU notional > 500% of equity) and get auto-deleveraged
        # 100ms later. Trims the lot to fit, or skips when the cap is
        # already saturated.
        sector_fit_info: dict | None = None
        try:
            from portfolio.sector_cap_fit import fit_lot_to_sector_cap
            open_q = {"user_id": user_id, "status": {"$in": ["open", "pending"]}}
            if cfg_account_id:
                open_q["account_id"] = cfg_account_id
            open_book_for_cap = await db.trades.find(open_q).to_list(length=200)
            fit = fit_lot_to_sector_cap(
                new_symbol=sym, new_lot=effective_lot,
                new_entry_price=float(signal["entry_price"]),
                open_positions=open_book_for_cap,
                equity=float(target_account.get("equity")
                             or target_account.get("balance") or 0),
                cfg=cfg,
            )
            sector_fit_info = fit
            if fit["lot"] <= 0:
                logger.warning("Sector-cap skip acct=%s sym=%s — %s",
                               cfg_account_id or "default", sym, fit["reason"])
                await _record_pulse(db, cfg, symbol=sym,
                    action="SKIP", level="warn",
                    reason=f"Sector-cap fit: {fit['reason']}",
                )
                continue
            if fit["lot"] < effective_lot:
                logger.info(
                    "Sector-cap trim acct=%s sym=%s lot=%s → %s · %s",
                    cfg_account_id or "default", sym,
                    effective_lot, fit["lot"], fit["reason"],
                )
                effective_lot = fit["lot"]
                sizing_method = sizing_method + "+sector_cap_fit"
        except Exception as e:  # noqa: BLE001
            logger.debug("Sector-cap fit skipped (%s) — proceeding without trim", e)

        engine = engine_for_account(target_account)
        trade_doc = await engine.execute(
            user_id=user_id,
            account=target_account,
            signal={
                "signal_id": signal_id,
                "symbol": signal["symbol"],
                "action": signal["action"],
                "lot_size": effective_lot,
                "entry_price": signal["entry_price"],
                "stop_loss": signal["stop_loss"],
                "take_profit": signal["take_profit"],
                "origin": "auto",
            },
            max_concurrent=max_concurrent,
            cfg_account_id=cfg_account_id,
        )
        if trade_doc.get("blocked"):
            logger.warning(
                "Auto-execute blocked user=%s acct=%s sym=%s reason=%s",
                user_id, cfg_account_id or "default", sym, trade_doc.get("blocked"),
            )
            await _record_pulse(db, cfg, symbol=sym,
                action="BLOCKED", level="block",
                reason=f"Safety Guardian blocked execution: {trade_doc.get('blocked')}",
            )
            continue
        logger.info("Bot auto-execute user=%s acct=%s sym=%s trade=%s",
                    user_id, cfg_account_id or "default", sym, trade_doc.get("id"))
        await db.signals.update_one({"_id": ObjectId(signal_id)}, {"$set": {
            "consumed": True,
            **({"corr_kelly_trim": corr_kelly_info} if corr_kelly_info else {}),
            **({"sector_cap_fit": sector_fit_info} if sector_fit_info else {}),
        }})
        await _record_pulse(db, cfg, symbol=sym,
            action="EXEC", level="info",
            reason=f"Executed {signal['action']} {sym} {effective_lot} lots @ {signal.get('entry_price')} (conf {signal.get('confidence')}%).",
        )
        inflight += 1


async def _process_user(db, cfg: dict):
    """Backward-compat shim — routes to the new per-config processor."""
    await _process_user_account(db, cfg)


async def loop():
    """Long-running asyncio task. Iterates over all active bots every N seconds."""
    interval = _loop_interval()
    logger.info("Bot runner started — interval=%ss, cooldown=%smin", interval, _cooldown_minutes())
    while True:
        try:
            db = get_db()
            # Pick up both fully-active bots AND paper-shadow bots (the latter
            # run the full signal pipeline but never execute — they log "what
            # would have happened" so users can A/B test their config safely.
            cursor = db.bot_configs.find({
                "$or": [{"active": True}, {"paper_shadow_mode": True}],
            })
            configs = await cursor.to_list(length=200)
            if configs:
                await asyncio.gather(*[_process_user(db, c) for c in configs],
                                     return_exceptions=True)
            # Settle paper trades against live prices (SL/TP hits)
            try:
                closed = await settle_paper_trades_against_price()
                if closed:
                    logger.info("Paper sweep closed %d trades", closed)
            except Exception as e:
                logger.exception("Paper sweep failed: %s", e)
            # Evaluate NL conditional triggers
            try:
                sweep = await sweep_triggers()
                if sweep.get("fired"):
                    logger.info("Trigger sweep fired=%d", sweep["fired"])
            except Exception as e:
                logger.exception("Trigger sweep failed: %s", e)
            # SL-imminent Telegram alerts (ETA < 5 min)
            try:
                sl_sweep = await sweep_sl_imminent()
                if sl_sweep.get("fired"):
                    logger.info("SL-imminent sweep fired=%d checked=%d",
                                sl_sweep["fired"], sl_sweep["checked"])
            except Exception as e:
                logger.exception("SL-imminent sweep failed: %s", e)
            # Pre-news existing-position protector — flatten open trades into
            # imminent HIGH-impact macro events (NFP/CPI/FOMC).
            try:
                protected = await sweep_pre_news(db)
                if protected:
                    logger.warning("Pre-news protect flattened %d trade(s)", protected)
            except Exception as e:
                logger.exception("Pre-news protect sweep failed: %s", e)
            # Autonomous portfolio deleveraging — fires on hard DD, sector cap,
            # combined-corr bucket, or VaR breach. Per-account cooldown built-in.
            try:
                dl = await sweep_auto_deleverage(db)
                if dl.get("triggered"):
                    logger.warning(
                        "Auto-deleverage sweep · checked=%d triggered=%d closed=%d",
                        dl["checked"], dl["triggered"], dl["closed"],
                    )
            except Exception as e:
                logger.exception("Auto-deleverage sweep failed: %s", e)
            # Self-Improving Research Agent — once-per-24h per user. The
            # per-user cooldown is enforced inside `run_for_user` so cheap to
            # call on every tick; >99% of calls return immediately.
            try:
                rs = await sweep_research_agent(db)
                if rs.get("ran"):
                    logger.warning(
                        "Self-improve sweep · ran=%d users · proposals=%d",
                        rs["ran"], rs["proposals_total"],
                    )
            except Exception as e:
                logger.exception("Self-improve sweep failed: %s", e)
            # ADWIN drift detection on the learned-meta residual stream
            # (iter-52). Detector is internally cooldown-gated so this is a
            # cheap no-op on most ticks.
            try:
                from drift_detector import maybe_trigger_retrain
                dr = await maybe_trigger_retrain(db)
                if dr.get("retrained"):
                    logger.warning(
                        "ADWIN drift → learned_meta retrained · result=%s",
                        dr.get("retrain_result", {}).get("trained"),
                    )
            except Exception as e:
                logger.exception("Drift detection sweep failed: %s", e)
        except Exception as e:
            logger.exception("Bot runner tick failed: %s", e)
        await asyncio.sleep(interval)
