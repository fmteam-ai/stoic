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
import contextvars
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
from friday_flat import sweep_all as sweep_friday_flat, in_friday_flat_window
from subscription_service import is_active as subscription_active
from auto_tune import get_auto_threshold
from intelligence_counters import increment as inc_intel_counter
from risk import get_profile, compute_lot_for_account
from portfolio.auto_deleverage import sweep as sweep_auto_deleverage
from portfolio.correlation_kelly import compute_correlation_aware_scale
from research_agent.self_improver import daily_sweep as sweep_research_agent

logger = logging.getLogger("bot-runner")
_CURRENT_SIGNAL: contextvars.ContextVar = contextvars.ContextVar("current_signal")

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


async def _on_sl_cooldown(db, user_id: str, symbol: str, lookback_min: int,
                          account_id: str | None = None) -> dict | None:
    """If the most recent trade for (user, symbol) was a stop-out within
    `lookback_min` minutes, return that trade's metadata. Used to suppress
    revenge re-entries into the same losing regime.
    """
    if lookback_min <= 0:
        return None
    q = {
        "user_id": user_id,
        "symbol": symbol,
        "status": "closed",
        "origin": "auto",
        "close_reason": "stop_loss",
    }
    if account_id:
        q["account_id"] = account_id
    cursor = db.trades.find(q).sort("closed_at", -1).limit(1)
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
    signal: dict | None = None,
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
        # Routine 5-min cooldown SKIPs overwrite the pulse every cycle and
        # bury the real blocker (anti-tilt, noise, risk cap). Keep the last
        # NOTABLE verdict separately so the UI can always show the true "why".
        update = {"_last_pulse": pulse}
        is_routine = action == "SKIP" and "cooldown active" in (reason or "").lower()
        if not is_routine:
            update["_last_notable_pulse"] = pulse
        await db.bot_configs.update_one(
            {"_id": cfg["_id"]},
            {"$set": update},
        )
        # iter-134 · Permanent decision ledger: every non-routine rejection
        # becomes an immutable audit record (the pulse above is overwritten
        # each cycle — the ledger never is).
        if action in ("SKIP", "BLOCKED") and not is_routine:
            from trade_decisions import record_decision, infer_stage
            _sig = signal or _CURRENT_SIGNAL.get(None)
            if _sig is not None and _sig.get("symbol") and symbol and _sig.get("symbol") != symbol:
                _sig = None  # stale context from another symbol — don't mislabel
            await record_decision(
                db, user_id=str(cfg.get("user_id") or ""), symbol=symbol or "",
                status="rejected", stage=infer_stage(reason), reason=reason,
                cfg=cfg, signal=_sig)
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

    # 0a. User moderation gate — suspended/terminated accounts NEVER trade.
    try:
        user_doc = await db.users.find_one({"_id": ObjectId(user_id)})
    except Exception:
        user_doc = None
    if user_doc:
        ustatus = user_doc.get("status") or "active"
        if ustatus in ("suspended", "terminated"):
            # Defence-in-depth: also flip cfg inactive so this isn't re-tried.
            await db.bot_configs.update_one(
                {"_id": cfg["_id"]},
                {"$set": {
                    "active": False,
                    "deactivated_reason": f"user_{ustatus}",
                    "deactivated_at": datetime.now(timezone.utc).isoformat(),
                }},
            )
            await _record_pulse(db, cfg,
                action="BLOCKED", level="block",
                reason=f"Account {ustatus} — bot disabled per STOIC Terms of Use.",
            )
            return

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

    # 1c. Friday Flat window (iter-52) — no NEW entries in the final stretch
    # before the Friday 21:00 UTC weekly close. Open positions are handled by
    # sweep_friday_flat in the main loop.
    ff = in_friday_flat_window(cfg)
    if ff["in_window"]:
        await _record_pulse(db, cfg,
            action="BLOCKED", level="block",
            reason=(f"Friday Flat window — no new entries within "
                    f"{ff['minutes_before']}min of the weekly close (Fri 21:00 UTC). "
                    "Weekend gap protection."),
        )
        return

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

    # iter-74 · Phase 3 — Regime-aware auto-preset overlay.
    # When `auto_preset_enabled` is on, pick the preset matching the LAST
    # tick's regime/execution-mode and overlay its config knobs onto cfg
    # in-memory. Uses cached regime (from previous tick) to avoid a
    # chicken-and-egg cycle where the signal-needed-to-pick-preset depends
    # on the preset-needed-to-shape-the-signal.
    auto_preset_active = bool(cfg.get("auto_preset_enabled"))
    if auto_preset_active:
        from adaptive_mode import pick_preset_for_regime
        from strategy_presets import PRESETS as _PRESETS
        pick = pick_preset_for_regime(
            cfg.get("_last_execution_mode"),
            cfg.get("_last_regime"),
        )
        preset_cfg = (_PRESETS.get(pick["preset_key"]) or {}).get("config", {})
        if preset_cfg:
            cfg = {**cfg, **preset_cfg,
                   "active_preset": f"{pick['preset_key']}_auto",
                   "_auto_preset_source": pick["preset_key"]}
            logger.info("Auto-preset overlay user=%s acct=%s → %s (%s)",
                        user_id, cfg_account_id or "default",
                        pick["preset_key"], pick["reason"])

    # iter-109 · Meta-Learning strategy switcher — bandit over presets scored
    # on the user's own recent trades. Overrides auto_preset when enabled.
    if cfg.get("meta_strategy_enabled"):
        try:
            from meta_strategy import apply_meta_strategy
            cfg, _meta_info = await apply_meta_strategy(db, user_id, cfg)
            if _meta_info and _meta_info["switched"]:
                await _record_pulse(db, cfg, symbol="*", action="ADAPT",
                    level="info",
                    reason=f"Meta-strategy: {_meta_info['reason']}")
                await inc_intel_counter(user_id, "meta_strategy_switch")
        except Exception as e:  # noqa: BLE001
            logger.exception("meta strategy failed user=%s: %s", user_id, e)

    max_concurrent = int(cfg.get("max_concurrent_trades", 3))
    max_lot_cap = float(cfg.get("max_lot_size") or 0.0)
    auto_tune_enabled = bool(cfg.get("auto_tune_enabled", True))
    min_conf_override = int(cfg.get("min_confidence_override") or 0)
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
        # iter-122 · origin=auto: judge the bot by its OWN trades — a $-0.16
        # manual close must not freeze auto-execution.
        recent_q = {"user_id": user_id, "status": "closed", "origin": "auto"}
        if cfg_account_id:
            recent_q["account_id"] = cfg_account_id
        recent = await db.trades.find(recent_q).sort(
            "closed_at", -1
        ).limit(anti_tilt_n).to_list(length=anti_tilt_n)
        # A trade counts as a LOSS only when pnl is strictly negative.
        # Reconciler ghost-closes (exit never reported → pnl 0.0 / None) and
        # true breakevens must NOT trip the freeze — users were frozen on
        # days with zero actual losing trades (iter-45 bug).
        if len(recent) == anti_tilt_n and all(float(r.get("pnl") or 0) < 0 for r in recent):
            last_close = recent[0].get("closed_at")
            try:
                lc = datetime.fromisoformat(str(last_close).replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - lc < timedelta(hours=anti_tilt_hours):
                    anti_tilt_active = True
                    logger.warning("Anti-tilt active user=%s acct=%s — last %d trades lost",
                                   user_id, cfg_account_id or "default", anti_tilt_n)
            except Exception:
                pass

    # Count current open + pending BOT trades to respect max_concurrent
    # (per-account when scoped). origin=auto only — manual trades must NEVER
    # consume the bot's slots.
    inflight_q = {"user_id": user_id, "status": {"$in": ["pending", "open"]},
                  "origin": "auto"}
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
                "origin": "auto",
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
            sl_cd = await _on_sl_cooldown(db, user_id, sym, sl_cooldown_min,
                                          account_id=cfg_account_id)
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
            _CURRENT_SIGNAL.set(signal)  # iter-136 · auto-snapshot for the decision ledger
            if not signal:
                await _record_pulse(db, cfg, symbol=sym,
                    action="HOLD", level="warn",
                    reason="Analyser pipeline returned no signal (Strategy or Risk agent failed). Check logs.",
                )
                continue
            # iter-121 · Scalp Radar — Telegram ping when M15 alignment arms.
            # State keyed per user+symbol, so 5 configs on XAUUSD ping once.
            try:
                from scalp_radar import scalp_radar_ping
                await scalp_radar_ping(db, user_id, sym, signal.get("intraday_m15"))
            except Exception as e:  # noqa: BLE001
                logger.debug("scalp radar failed: %s", e)
            # iter-74 · Cache the regime so the NEXT tick can pick a preset
            # for auto_preset_enabled without re-querying the orchestrator.
            try:
                rexec = (signal.get("regime_execution_mode") or {}).get("execution_mode")
                rname = (signal.get("regime") or {}).get("regime")
                if rexec or rname:
                    await db.bot_configs.update_one(
                        {"_id": cfg["_id"]},
                        {"$set": {"_last_execution_mode": rexec,
                                  "_last_regime": rname}},
                    )
            except Exception:  # noqa: BLE001
                pass
            # iter-74 · Phase 1 — Profit-Taking Mode + TP cap (swing-era).
            # iter-128: NEVER reshape deterministic intraday engines — their
            # TP=2×SL geometry IS the strategy; clipping the TP inverted the
            # realized R:R (avg win < avg loss) and nullified breakeven.
            try:
                from strategy_engines import DETERMINISTIC_INTRADAY_SCOPES as _DET_PT
                if signal.get("scope") not in _DET_PT:
                    from adaptive_mode import apply_profit_taking_mode
                    signal, _eff_cfg_pt = apply_profit_taking_mode(signal, cfg)
            except Exception as e:  # noqa: BLE001
                logger.warning("apply_profit_taking_mode failed sym=%s: %s — using raw signal", sym, e)
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
                if 0 < min_conf_override < 100:
                    eff = min(eff, float(min_conf_override))
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

        # iter-55 · Active auto-guards (Daily Auto-Learning) — evidence-gated
        # measures applied by the loss advisor, enforced with the exact
        # predicates that were shadow-tested. Auto-reverted when evidence
        # turns negative on fresh data.
        if signal.get("action") in ("BUY", "SELL"):
            guards = await db.auto_guards.find(
                {"user_id": user_id, "active": True}).to_list(length=10)
            if guards:
                from loss_advisor import live_guard_block
                gb = live_guard_block(signal, guards)
                if gb:
                    await _record_pulse(db, cfg, symbol=sym,
                        action="SKIP", level="warn", reason=gb)
                    await inc_intel_counter(user_id, "auto_guard_block")
                    continue

        # iter-56 · EOD quiet window — spreads widen drastically in the final
        # minutes before the broker's daily close. Mirrors the EA v1.41
        # client-side guard: no new signal execution 23:40-00:05 broker time.
        if signal.get("action") in ("BUY", "SELL"):
            from eod_quiet import eod_quiet_block
            qb = eod_quiet_block(connected[0] if connected else None)
            if qb:
                await _record_pulse(db, cfg, symbol=sym,
                    action="SKIP", level="warn", reason=qb)
                await inc_intel_counter(user_id, "eod_quiet_block")
                continue

        # iter-57 · Payoff guard — fixes swing-signal TP/SL inversions.
        # iter-128: deterministic intraday engines keep their own geometry.
        from strategy_engines import DETERMINISTIC_INTRADAY_SCOPES as _DET_PG
        if (signal.get("action") in ("BUY", "SELL")
                and signal.get("scope") not in _DET_PG):
            from payoff_guard import payoff_guard_apply
            pg = payoff_guard_apply(signal, cfg)
            if pg and pg.get("skip"):
                await _record_pulse(db, cfg, symbol=sym,
                    action="SKIP", level="warn", reason=pg["skip"])
                await inc_intel_counter(user_id, "payoff_guard_veto")
                continue
            if pg and pg.get("tighten"):
                signal["stop_loss"] = pg["tighten"]
                try:
                    signal["sl_pips"] = round(float(signal.get("sl_pips") or 0)
                                              * pg["sl_pips_scale"], 1)
                except (TypeError, ValueError):
                    pass
                signal["payoff_guard"] = {"tightened": True, "reason": pg["reason"]}
                await inc_intel_counter(user_id, "payoff_guard_tighten")

        # iter-58 · Loss cooldown — after a BOT loss, same symbol+direction
        # re-entries are paused for 30min on THAT account (iter-122b:
        # per-account — each account has its own equity and settings).
        if signal.get("action") in ("BUY", "SELL"):
            from loss_cooldown import loss_cooldown_block
            lc = await loss_cooldown_block(db, user_id, sym, signal["action"], cfg)
            if lc:
                await _record_pulse(db, cfg, symbol=sym,
                    action="SKIP", level="warn", reason=lc)
                await inc_intel_counter(user_id, "loss_cooldown_block")
                continue

        # iter-60 · Structure / Range / Fed-tone gates (Market Structure,
        # Quant and Macro agents). All fail-open: missing data → no veto.
        if signal.get("action") in ("BUY", "SELL"):
            from market_structure import structure_snapshot, structure_gate
            from range_forecast import build_range_forecast, range_gate
            from fed_tone import get_fed_tone, fed_tone_gate
            from pip_utils import base_symbol as _bs
            _base = _bs(sym)
            cdoc = await db.intraday_candles.find_one(
                {"user_id": user_id, "symbol": _base})
            snap = structure_snapshot((cdoc or {}).get("bars") or [])
            signal["market_structure"] = snap
            # fade-style scalps oppose fresh structure BY DESIGN; their
            # protection is the tight M15-ATR stop + 0.25% risk cap
            from strategy_engines import SCALP_ENGINES as _fade_exempt
            sg = (structure_gate(signal["action"], snap)
                  if signal.get("scope") not in (_fade_exempt | {"range_fade"})
                  else None)
            if sg:
                await _record_pulse(db, cfg, symbol=sym,
                    action="SKIP", level="warn", reason=sg)
                await inc_intel_counter(user_id, "structure_gate_veto")
                continue

            # iter-129 · Session-trend / exhaustion gates + trend-ride mode
            # (2026-07-13 review: counter-trend buys into an 85pt gold slide,
            # selling the day low after -2%, and 12pt TPs in an 80pt trend).
            from payoff_guard import (session_trend_gate, exhaustion_chase_gate,
                                      trend_ride_check)
            from intraday_features import compute_intraday_features
            _feats = compute_intraday_features((cdoc or {}).get("bars") or [])
            if _feats:
                signal["session_feats"] = {k: _feats.get(k) for k in (
                    "day_range_pct", "range_pos_pct", "ema20_slope_pct_2h",
                    "swing_structure")}
                if cfg.get("session_trend_gate_enabled", True):
                    stg = session_trend_gate(signal["action"], _feats, _base)
                    if stg:
                        await _record_pulse(db, cfg, symbol=sym,
                            action="SKIP", level="warn", reason=stg,
                            signal=signal)
                        await inc_intel_counter(user_id, "session_trend_veto")
                        continue
                if cfg.get("exhaustion_gate_enabled", True):
                    exg = exhaustion_chase_gate(signal["action"], _feats, _base)
                    if exg:
                        await _record_pulse(db, cfg, symbol=sym,
                            action="SKIP", level="warn", reason=exg,
                            signal=signal)
                        await inc_intel_counter(user_id, "exhaustion_chase_veto")
                        continue
                if cfg.get("trend_ride_enabled", True):
                    ride = trend_ride_check(signal["action"], _feats, _base)
                    if ride:
                        try:
                            _entry = float(signal.get("entry_price") or 0)
                            _tp = float(signal.get("take_profit") or 0)
                            _mult = float(cfg.get("trend_ride_tp_mult") or 1.8)
                            if _entry and _tp and _mult > 1.0:
                                signal["take_profit"] = round(
                                    _entry + (_tp - _entry) * _mult, 5)
                                if signal.get("tp_pips"):
                                    signal["tp_pips"] = round(
                                        float(signal["tp_pips"]) * _mult, 1)
                                signal["trend_ride"] = ride
                                await inc_intel_counter(user_id, "trend_ride_applied")
                                await _record_pulse(db, cfg, symbol=sym,
                                    action="INFO", level="info",
                                    reason=(f"Trend-ride: {ride['day_range_pct']:.2f}% "
                                            f"{ride['direction']} day — TP widened ×{_mult} "
                                            f"so the trailing stop can ride the move."))
                        except (TypeError, ValueError):
                            pass
            try:
                from market import get_history
                rf = build_range_forecast(await get_history(sym),
                                          (cdoc or {}).get("bars") or [])
            except Exception:
                rf = None
            signal["range_forecast"] = rf
            from strategy_engines import DETERMINISTIC_INTRADAY_SCOPES as _DET
            _det_scope = signal.get("scope") in _DET
            rg = range_gate(signal["action"], signal.get("entry_price"),
                            signal.get("tp1") or signal.get("take_profit"), rf)
            if rg:
                # daily range forecast can't judge tiny deterministic
                # intraday targets — advisory for those scopes
                if not _det_scope:
                    await _record_pulse(db, cfg, symbol=sym,
                        action="SKIP", level="warn", reason=rg)
                    await inc_intel_counter(user_id, "range_gate_veto")
                    continue
                signal["range_gate_advisory"] = rg
            if _base == "XAUUSD":
                try:
                    tone = await get_fed_tone()
                except Exception:
                    tone = None
                signal["fed_tone"] = tone
                fg = fed_tone_gate(signal["action"], _base, tone)
                if fg:
                    # daily macro tone — advisory for deterministic scalps
                    if not _det_scope:
                        await _record_pulse(db, cfg, symbol=sym,
                            action="SKIP", level="warn", reason=fg)
                        await inc_intel_counter(user_id, "fed_tone_veto")
                        continue
                    signal["fed_tone_advisory"] = fg
                # iter-109 · Causal AI — structural transmission chain
                # (inflation → yields → real yields → USD → gold).
                try:
                    from causal_model import build_causal_view
                    from macro_feeds import get_macro_snapshot
                    signal["causal"] = build_causal_view(
                        await get_macro_snapshot(), tone)
                except Exception as e:  # noqa: BLE001
                    logger.debug("causal model skipped: %s", e)

            # iter-105 · Liquidity Mapping agent — order blocks, stop clusters,
            # volume profile, cumulative delta, DOM (EA v1.43). Fail-open.
            lmap = None
            try:
                from liquidity_map import build_liquidity_map, liquidity_gate
                dom_doc = await db.dom_snapshots.find_one(
                    {"user_id": user_id, "symbol": _base})
                lmap = build_liquidity_map(
                    (cdoc or {}).get("bars") or [],
                    price=signal.get("entry_price"), dom_doc=dom_doc)
            except Exception as e:  # noqa: BLE001
                logger.debug("liquidity map skipped: %s", e)
            if lmap:
                signal["liquidity"] = lmap
                lg = liquidity_gate(signal["action"], lmap,
                                    signal.get("entry_price"))
                if lg:
                    lq_mode = str(cfg.get("liquidity_gate_mode")
                                  or "enforce").lower()
                    if lq_mode == "enforce":
                        await _record_pulse(db, cfg, symbol=sym,
                            action="SKIP", level="warn", reason=lg)
                        await inc_intel_counter(user_id, "liquidity_gate_veto")
                        continue
                    signal["liquidity_advisory"] = lg

            # iter-106 · AI News Understanding — Claude scores each headline
            # (Reuters/Bloomberg/FOMC/CPI/NFP) -3..+3 for THIS asset. Fail-open.
            news_ai = None
            try:
                from news_understanding import get_news_understanding, news_gate
                news_ai = await get_news_understanding(_base)
            except Exception as e:  # noqa: BLE001
                logger.debug("news understanding skipped: %s", e)
            if news_ai:
                signal["news_ai"] = news_ai
                ng = news_gate(signal["action"], _base, news_ai)
                if ng:
                    news_mode = str(cfg.get("news_gate_mode")
                                    or "enforce").lower()
                    if news_mode == "enforce" and not _det_scope:
                        await _record_pulse(db, cfg, symbol=sym,
                            action="SKIP", level="warn", reason=ng,
                            signal=signal)
                        await inc_intel_counter(user_id, "news_gate_veto")
                        continue
                    signal["news_ai_advisory"] = ng

                # iter-130 · Narrative fusion — the news net score now feeds
                # the DECISION (confidence bias) and the RISK agent (sizing),
                # not just the extreme-veto (2026-07-13: US-Iran drove a 2%
                # gold slide the bot traded blind to).
                from news_understanding import (news_confidence_bias,
                                                narrative_risk_scale)
                _delta, _bias_note = news_confidence_bias(
                    signal["action"], news_ai)
                if _delta:
                    signal["confidence"] = max(5.0, min(95.0, round(
                        float(signal.get("confidence") or 0) + _delta, 1)))
                    signal["news_bias"] = {"delta": _delta, "note": _bias_note}
                    await inc_intel_counter(user_id, "news_bias_applied")
                _nscale, _nreason = narrative_risk_scale(
                    signal["action"], news_ai)
                if _nscale < 1.0:
                    signal["news_size_scale"] = _nscale
                    signal["news_size_reason"] = _nreason
                    await _record_pulse(db, cfg, symbol=sym,
                        action="INFO", level="info", reason=_nreason)
                    await inc_intel_counter(user_id, "news_size_trim")

            # iter-107 · Calendar Intelligence — predicts breakout/fakeout/
            # reversal/continuation for the next high-impact print. Vetoes
            # entries only when a fakeout (stop-hunt) is the dominant scenario.
            cal_pred = None
            try:
                from calendar_intel import (next_event_prediction,
                                            calendar_entry_policy)
                cal_pred = await next_event_prediction(
                    db, _base, (cdoc or {}).get("bars") or [], lmap)
            except Exception as e:  # noqa: BLE001
                logger.debug("calendar intel skipped: %s", e)
            if cal_pred:
                signal["calendar_intel"] = cal_pred
                pol = calendar_entry_policy(signal["action"], cal_pred)
                if pol:
                    signal["calendar_policy"] = pol
                    cal_mode = str(cfg.get("calendar_intel_mode")
                                   or "enforce").lower()
                    if pol["mode"] == "VETO" and cal_mode == "enforce":
                        await _record_pulse(db, cfg, symbol=sym,
                            action="SKIP", level="warn", reason=pol["reason"])
                        await inc_intel_counter(user_id, "calendar_intel_veto")
                        continue

        # iter-61 · Offline RL policy gate — distributional Q-values learned
        # from the user's real trades (reward = PnL − λ·loss − μ·drawdown).
        # advisory (default): annotate only · enforce: BLOCK skips, SCALE halves lot.
        if signal.get("action") in ("BUY", "SELL"):
            from rl_policy import get_policy, rl_decision
            rl_dec = None
            try:
                rl_dec = rl_decision(await get_policy(db, user_id), signal, sym)
            except Exception as e:  # noqa: BLE001
                logger.debug("RL policy skipped: %s", e)
            if rl_dec:
                signal["rl_policy"] = rl_dec
                rl_mode = str(cfg.get("rl_policy_mode") or "advisory").lower()
                if rl_dec["decision"] == "BLOCK" and rl_mode != "off":
                    await inc_intel_counter(user_id, "rl_policy_block_advice")
                    if rl_mode == "enforce":
                        await _record_pulse(db, cfg, symbol=sym,
                            action="SKIP", level="warn",
                            reason=f"RL policy: {rl_dec['reason']}")
                        await inc_intel_counter(user_id, "rl_policy_block")
                        continue
                elif rl_dec["decision"] == "SCALE" and rl_mode == "enforce":
                    signal["rl_scale"] = rl_dec.get("scale", 0.5)

        # iter-65 · Bayesian decision model — P(trade succeeds) + E[R] from
        # real outcomes (Beta-Binomial posterior per state). advisory default;
        # enforce vetoes quality-D setups with sufficient evidence.
        if signal.get("action") in ("BUY", "SELL"):
            bd = None
            try:
                from bayes_decision import get_model, bayes_decision, MIN_EVIDENCE
                bd = bayes_decision(await get_model(db, user_id), signal, sym)
            except Exception as e:  # noqa: BLE001
                logger.debug("bayes decision skipped: %s", e)
            if bd:
                signal["bayes"] = bd
                b_mode = str(cfg.get("bayes_gate_mode") or "advisory").lower()
                if bd["quality"] == "D" and bd["n"] >= MIN_EVIDENCE and b_mode != "off":
                    await inc_intel_counter(user_id, "bayes_d_quality")
                    if b_mode == "enforce":
                        await _record_pulse(db, cfg, symbol=sym,
                            action="SKIP", level="warn",
                            reason=(f"Bayesian decision gate: P(success) "
                                    f"{bd['p_success']:.0%} (CI {bd['ci90'][0]:.0%}-"
                                    f"{bd['ci90'][1]:.0%}, n={bd['n']}), E[reward] "
                                    f"{bd['expected_reward_r']}R vs E[loss] "
                                    f"{bd['expected_loss_r']}R → EV {bd['ev_r']}R "
                                    f"(quality D). Vetoed."))
                        await inc_intel_counter(user_id, "bayes_block")
                        continue

        # iter-62 · Forecast gate — Chronos-Bolt zero-shot quantile forecast.
        # Blocks only when the ENTIRE 80% band moves against the trade.
        # advisory (default): annotate + counter · enforce: skip. Fail-open.
        if signal.get("action") in ("BUY", "SELL"):
            fc = None
            try:
                from forecast_agent import get_forecast, forecast_gate
                fc = await get_forecast(db, user_id, sym)
            except Exception as e:  # noqa: BLE001
                logger.debug("forecast agent skipped: %s", e)
            if fc:
                signal["forecast"] = fc
                # iter-64 · Probabilistic trade evaluation — outcome
                # distribution → EV filter, quantile stop, dynamic sizing.
                # advisory (default): annotate · enforce: veto negative-EV,
                # apply tighter quantile SL, scale lot down.
                try:
                    from prob_forecast import trade_eval
                    pe = trade_eval(sym, signal["action"], signal.get("entry_price"),
                                    signal.get("stop_loss"),
                                    signal.get("tp1") or signal.get("take_profit"), fc)
                except Exception as e:  # noqa: BLE001
                    logger.debug("prob eval skipped: %s", e)
                    pe = None
                if pe:
                    signal["prob_eval"] = pe
                    pf_mode = str(cfg.get("prob_forecast_mode") or "advisory").lower()
                    if pe["negative_ev"]:
                        await inc_intel_counter(user_id, "prob_ev_negative")
                        if pf_mode == "enforce":
                            await _record_pulse(db, cfg, symbol=sym,
                                action="SKIP", level="warn",
                                reason=(f"Probabilistic EV gate: expected value "
                                        f"{pe['ev_pips']} pips, prob-weighted expectancy "
                                        f"{pe['prob_expectancy_pips']} pips (P(TP) "
                                        f"{pe['p_tp']:.0%} vs P(SL) {pe['p_sl']:.0%}) — "
                                        f"distribution says this trade loses. Vetoed."))
                            await inc_intel_counter(user_id, "prob_ev_block")
                            continue
                    if pf_mode == "enforce":
                        if pe.get("suggested_sl"):
                            signal["stop_loss"] = pe["suggested_sl"]
                            signal["prob_sl_applied"] = True
                        if pe["lot_multiplier"] < 1.0:
                            signal["prob_lot_scale"] = pe["lot_multiplier"]
                fgate = forecast_gate(signal["action"], fc)
                if fgate:
                    fc_mode = str(cfg.get("forecast_gate_mode") or "advisory").lower()
                    signal["forecast_gate_advice"] = fgate
                    await inc_intel_counter(user_id, "forecast_gate_advice")
                    if fc_mode == "enforce":
                        await _record_pulse(db, cfg, symbol=sym,
                            action="SKIP", level="warn", reason=fgate)
                        await inc_intel_counter(user_id, "forecast_gate_block")
                        continue

        # iter-108 · Stacked ML Ensemble — GBM/XGBoost/CatBoost/LightGBM
        # (skill-weighted by walk-forward AUC) + Transformer + RL + Bayes,
        # intelligently averaged into one P(win). Fail-open.
        if signal.get("action") in ("BUY", "SELL"):
            ml_pred = None
            try:
                from ml_ensemble import ml_predict, ml_gate
                ml_pred = await ml_predict(db, user_id, signal, sym)
            except Exception as e:  # noqa: BLE001
                logger.debug("ml ensemble skipped: %s", e)
            if ml_pred:
                signal["ml_ensemble"] = ml_pred
                mg = ml_gate(ml_pred)
                if mg:
                    ml_mode = str(cfg.get("ml_ensemble_mode")
                                  or "advisory").lower()
                    await inc_intel_counter(user_id, "ml_ensemble_low_p")
                    if ml_mode == "enforce":
                        await _record_pulse(db, cfg, symbol=sym,
                            action="SKIP", level="warn", reason=mg)
                        await inc_intel_counter(user_id, "ml_ensemble_block")
                        continue
                    signal["ml_ensemble_advisory"] = mg

        # iter-63 · Master Agent consensus — weighted vote across all agents
        # (trend/quant/structure/forecast/macro). Trade fires only when the
        # score clears `consensus_threshold` (default 55; mode default enforce).
        if signal.get("action") in ("BUY", "SELL"):
            from consensus import compute_consensus, DEFAULT_THRESHOLD
            cons = compute_consensus(signal)
            signal["consensus"] = cons
            c_mode = str(cfg.get("consensus_gate_mode") or "enforce").lower()
            c_thr = int(cfg.get("consensus_threshold") or DEFAULT_THRESHOLD)
            if cons["score"] < c_thr and c_mode != "off":
                await inc_intel_counter(user_id, "consensus_low")
                from strategy_engines import DETERMINISTIC_INTRADAY_SCOPES as _DET_C
                if c_mode == "enforce" and signal.get("scope") not in _DET_C:
                    detail = ", ".join(f"{k} {v:+.1f}" for k, v in cons["votes"].items())
                    await _record_pulse(db, cfg, symbol=sym,
                        action="SKIP", level="warn",
                        reason=(f"Master Agent consensus {cons['score']}/100 "
                                f"({cons['verdict']}) below threshold {c_thr} — "
                                f"votes: {detail}."))
                    await inc_intel_counter(user_id, "consensus_block")
                    continue
                # deterministic intraday engines: daily-agent votes are
                # advisory — the persona's own rules + tight stop govern
                signal["consensus_advisory"] = (
                    f"consensus {cons['score']}/100 below {c_thr} (advisory)")

        # iter-113 · Self-evaluation behavior adjustments — recurring
        # mistakes learned from graded trades reshape new candidates.
        # iter-128: swing/MTF only — widening a scalp's SL without moving its
        # TP broke the 2:1 math, and min_conf bumps would blanket-block
        # deterministic engines (their confidence is synthetic min+5).
        if (signal.get("action") in ("BUY", "SELL")
                and signal.get("scope") not in _DET_PG):
            try:
                from self_evaluation import get_adjustments, apply_adjustments
                _adj = await get_adjustments(db, user_id)
                if _adj:
                    _applied = apply_adjustments(signal, _adj)
                    if _applied:
                        signal["behavior_adjustments"] = _applied
                        logger.info("Behavior adjustments applied user=%s: %s",
                                    user_id, _applied)
            except Exception as e:  # noqa: BLE001
                logger.debug("behavior adjustments skipped: %s", e)

        # iter-118 · Final R:R guard — re-check geometry AFTER every overlay
        # (Smart Cap TP clip, payoff-guard tighten, behavior adjustments).
        # The signal-time R:R veto only sees the ORIGINAL geometry.
        if signal.get("action") in ("BUY", "SELL"):
            from payoff_guard import final_rr_guard
            frr = final_rr_guard(signal, cfg)
            if frr:
                await _record_pulse(db, cfg, symbol=sym,
                    action="SKIP", level="warn", reason=frr)
                await inc_intel_counter(user_id, "final_rr_veto")
                continue

        # iter-110 · Uncertainty estimation — calibrated confidence + risk
        # tier from model disagreement, CI width, band dispersion and agent
        # conflict. Skips low-confidence trades (default enforce).
        if signal.get("action") in ("BUY", "SELL"):
            from uncertainty import estimate_uncertainty, uncertainty_gate
            est = estimate_uncertainty(signal)
            signal["uncertainty"] = est
            u_mode = str(cfg.get("uncertainty_gate_mode") or "enforce").lower()
            ug = uncertainty_gate(
                est, int(cfg.get("min_calibrated_confidence") or 60)
                + int(signal.get("_conf_floor_bump") or 0))
            if ug and u_mode != "off":
                await inc_intel_counter(user_id, "uncertainty_low_conf")
                from strategy_engines import DETERMINISTIC_INTRADAY_SCOPES
                if (u_mode == "enforce"
                        and signal.get("scope") not in DETERMINISTIC_INTRADAY_SCOPES):
                    await _record_pulse(db, cfg, symbol=sym,
                        action="SKIP", level="warn", reason=ug)
                    await inc_intel_counter(user_id, "uncertainty_skip")
                    continue
                signal["uncertainty_advisory"] = ug

        # iter-112 · Monte Carlo trade simulation — 10k bootstrap paths from
        # real M15 dynamics: P(TP first) vs P(SL first), max-DD distribution.
        # Enter only if expected value is positive (default enforce).
        if signal.get("action") in ("BUY", "SELL"):
            mc = None
            try:
                from monte_carlo import simulate_trade, mc_gate, typical_cost
                from pip_utils import base_symbol as _bs_mc
                _cdoc_mc = await db.intraday_candles.find_one(
                    {"user_id": user_id, "symbol": _bs_mc(sym)}, {"bars": 1})
                # simulate the BLENDED target (50%@TP1+25%@TP2+25%@TP3 =
                # rr_ratio × SL) — simulating TP1 alone (0.8×SL) wrongly
                # tags every 3-TP trade as negative EV
                _entry_mc = signal.get("entry_price")
                _sl_mc = signal.get("stop_loss")
                _tp_mc = signal.get("tp1") or signal.get("take_profit")
                _rr_mc = float(signal.get("rr_ratio") or 0)
                if _entry_mc and _sl_mc and _rr_mc > 0:
                    _d = abs(_entry_mc - _sl_mc) * _rr_mc
                    _tp_mc = (_entry_mc + _d if signal["action"] == "BUY"
                              else _entry_mc - _d)
                mc = simulate_trade(
                    signal["action"], _entry_mc,
                    _sl_mc,
                    _tp_mc,
                    (_cdoc_mc or {}).get("bars") or [],
                    n_paths=int(cfg.get("monte_carlo_paths") or 10000),
                    cost_price=typical_cost(_bs_mc(sym), _entry_mc))
            except Exception as e:  # noqa: BLE001
                logger.debug("monte carlo skipped: %s", e)
            if mc:
                signal["monte_carlo"] = mc
                mg = mc_gate(mc)
                if mg:
                    mc_mode = str(cfg.get("monte_carlo_mode")
                                  or "enforce").lower()
                    await inc_intel_counter(user_id, "mc_negative_ev")
                    # IID bootstrap inherits recent drift → it can't price
                    # mean-reversion; fade entries get advisory treatment
                    if mc_mode == "enforce" and signal.get("entry_style") != "fade":
                        await _record_pulse(db, cfg, symbol=sym,
                            action="SKIP", level="warn", reason=mg,
                            signal=signal)
                        await inc_intel_counter(user_id, "mc_block")
                        continue
                    signal["monte_carlo_advisory"] = mg

        signal["user_id"] = user_id
        # iter-113 · Explainable AI — human-readable decision rationale.
        if signal.get("action") in ("BUY", "SELL"):
            try:
                from explainer import explain_decision
                signal["explanation"] = explain_decision(signal)
            except Exception as e:  # noqa: BLE001
                logger.debug("explainer skipped: %s", e)
        if cfg.get("meta_strategy_active"):
            signal["meta_strategy"] = cfg["meta_strategy_active"]
        if cfg_account_id:
            signal["account_id"] = cfg_account_id  # signal tagged so UI can filter
        signal["consumed"] = False
        signal["created_at"] = datetime.now(timezone.utc).isoformat()
        signal["origin"] = "shadow" if shadow_only else "auto"
        # iter-74 · audit trail for adaptive overlays
        if auto_preset_active:
            signal["adaptive_auto_preset"] = {
                "selected": cfg.get("_auto_preset_source"),
                "active_preset": cfg.get("active_preset"),
            }
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
            "origin": "auto",
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
        ls_q = {
            "user_id": user_id, "symbol": sym, "action": signal["action"],
            "status": "closed", "origin": "auto", "closed_at": {"$gte": cutoff},
        }
        if cfg.get("account_id"):
            ls_q["account_id"] = cfg["account_id"]
        recent_closed = await db.trades.find(ls_q).sort(
            "closed_at", -1).limit(LOSS_STREAK_THRESHOLD).to_list(LOSS_STREAK_THRESHOLD)
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
        # iter-74 · Phase 2 — Rolling adaptive risk multiplier.
        # Scales profile.risk_pct by a multiplier derived from the last N
        # closed trades' win rate. Multiplier ∈ [0.5, 1.3]. Neutral when
        # fewer than 5 closed samples exist on the account.
        adaptive_risk_info: dict | None = None
        # iter-111 · Adaptive Position Sizing — risk% scales with confidence,
        # volatility, recent accuracy, liquidity and drawdown. Supersedes the
        # older win-rate-only adaptive_risk when enabled (default ON).
        if cfg.get("adaptive_sizing_enabled", True):
            try:
                from adaptive_sizing import compute_adaptive_risk
                asz = await compute_adaptive_risk(
                    db, user_id, cfg, signal, target_account,
                    float(profile.get("risk_pct") or 1.0), cfg_account_id)
                profile = {**profile, "risk_pct": asz["risk_pct"]}
                signal["adaptive_sizing"] = asz
                logger.info(
                    "Adaptive sizing user=%s sym=%s base=%.2f%% × %.2f → "
                    "%.2f%% · %s", user_id, sym, asz["base_risk_pct"],
                    asz["multiplier"], asz["risk_pct"], asz["components"])
            except Exception as e:  # noqa: BLE001
                logger.warning("adaptive sizing failed: %s — using base risk", e)
        elif cfg.get("adaptive_risk_enabled"):
            try:
                from adaptive_mode import compute_risk_multiplier
                adaptive_risk_info = await compute_risk_multiplier(
                    user_id=user_id, account_id=cfg_account_id,
                    window=int(cfg.get("adaptive_risk_window") or 20),
                )
                mult = float(adaptive_risk_info.get("multiplier") or 1.0)
                if mult != 1.0:
                    profile = {**profile,
                               "risk_pct": float(profile.get("risk_pct") or 0) * mult}
                    logger.info(
                        "Adaptive risk mult=%.2f×  win_rate=%s%%  samples=%s  user=%s acct=%s",
                        mult, adaptive_risk_info.get("win_rate_pct"),
                        adaptive_risk_info.get("samples"),
                        user_id, cfg_account_id or "default",
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("adaptive_risk lookup failed: %s — using base risk", e)
        # Iter-65: pass today's locked daily-profit so it's removed from the
        # equity pool used for Kelly sizing. The locked $ becomes untouchable.
        from profit_target import locked_profit_amount
        locked = locked_profit_amount(cfg)
        # iter-127 · HF scalp engines carry a per-trade risk cap (0.25%)
        rcap = signal.get("risk_pct_cap")
        if rcap:
            profile = {**profile,
                       "risk_pct": min(float(profile.get("risk_pct") or 0), float(rcap))}
        # iter-133 · Kelly disabled by default (quant review C1): engine
        # confidence is a setup score, not a calibrated probability. Fixed
        # fractional risk until calibration diagnostics exist. Re-enable
        # per-account via cfg `kelly_enabled: true`.
        _kelly_on = bool(cfg.get("kelly_enabled", False))
        sized = compute_lot_for_account(
            account=target_account,
            symbol=signal["symbol"],
            entry_price=signal["entry_price"],
            stop_loss=signal["stop_loss"],
            confidence_pct=float(signal.get("confidence") or 0),
            profile=profile,
            locked_profit=locked,
            kelly_enabled=_kelly_on,
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
        if _kelly_on and max_lot_cap > 0 and kelly_cap > 0:
            conf_scale = min(kelly_f / kelly_cap, 1.0) if kelly_f > 0 else 0.0
            scaled_lot = max(round(max_lot_cap * conf_scale, 2), 0.01)
            # Pick the smaller of: absolute Kelly lot vs confidence-scaled cap.
            effective_lot = min(absolute_lot, scaled_lot)
            sizing_method = "max_cap_kelly_scaled"
        else:
            effective_lot = absolute_lot
            # Hard-ceiling clamp (fixed-fraction mode or kelly_cap=0)
            if max_lot_cap > 0 and effective_lot > max_lot_cap:
                effective_lot = max_lot_cap
            sizing_method = sized.get("method") or "absolute_kelly"

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

        # iter-61 · RL policy half-size (enforce mode SCALE decision)
        # iter-64 · combined with probabilistic-forecast lot multiplier
        # iter-130 · combined with narrative risk scale (news fusion)
        _rl_scale = float(signal.get("rl_scale") or 1.0) \
            * float(signal.get("prob_lot_scale") or 1.0) \
            * float(signal.get("news_size_scale") or 1.0)
        if _rl_scale < 1.0:
            effective_lot = max(round(effective_lot * _rl_scale, 2), 0.01)
            sizing_method = sizing_method + "+rl_scale"
            if signal.get("news_size_scale"):
                sizing_method = sizing_method + "+narrative"

        # iter-139 · RL capital allocator — per-engine capital weight learned
        # from recent risk-adjusted performance. Shrink-only (weight ≤ 1.0).
        # A lookup failure means full weight; the fail-closed risk engine
        # below stays the final pre-trade authority.
        try:
            from rl_allocator import allocator_weight_for
            alloc = await allocator_weight_for(db, user_id, signal.get("scope"), cfg)
            await db.signals.update_one(
                {"_id": result.inserted_id}, {"$set": {"allocator": alloc}})
            if alloc.get("mode") == "enforce" and float(alloc.get("weight") or 1.0) < 1.0:
                effective_lot = max(round(effective_lot * float(alloc["weight"]), 2), 0.01)
                sizing_method = sizing_method + "+allocator"
                logger.info("RL allocator trim ×%.2f scope=%s user=%s: %s",
                            alloc["weight"], signal.get("scope"), user_id,
                            alloc.get("reason"))
        except Exception as e:  # noqa: BLE001
            logger.debug("rl allocator skipped: %s", e)

        # iter-114 · Advanced Risk Engine — final pre-trade authority:
        # drawdown ladder (D/W/M), abnormal-market halt, dynamic leverage,
        # event-window exposure cap, CVaR budget. Block or trim.
        if cfg.get("risk_engine_enabled", True):
            try:
                from risk_engine import risk_engine_evaluate
                rev = await risk_engine_evaluate(
                    db, user_id, cfg, target_account, signal,
                    effective_lot, cfg_account_id)
                await db.signals.update_one(
                    {"_id": result.inserted_id},
                    {"$set": {"risk_engine": {
                        "allow": rev["allow"], "scale": rev["scale"],
                        "checks": rev["checks"],
                        "pnl_windows": rev["pnl_windows"]}}})
                if not rev["allow"]:
                    await _record_pulse(db, cfg, symbol=sym,
                        action="SKIP", level="warn",
                        reason=f"Risk engine: {rev['blocked_by']['detail']}")
                    await inc_intel_counter(user_id, "risk_engine_block")
                    continue
                if rev["scale"] < 1.0:
                    effective_lot = max(
                        round(effective_lot * rev["scale"], 2), 0.01)
                    sizing_method = sizing_method + "+risk_engine"
                    logger.info(
                        "Risk engine trim ×%.3f user=%s sym=%s: %s",
                        rev["scale"], user_id, sym,
                        "; ".join(c["detail"] for c in rev["checks"]
                                  if c["status"] == "trim"))
            except Exception as e:  # noqa: BLE001
                logger.warning("risk engine failed (fail-open): %s", e)

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

        # iter-128 · Correlated-fade stagger — when the same symbol+direction
        # fade fired on another account in the last 30 min, mirror it only
        # once that trade is ≥ +0.3R in profit. Kills simultaneous
        # multi-account losses on one bad fade.
        if signal.get("entry_style") == "fade":
            try:
                from pip_utils import base_symbol as _bs_st
                from datetime import timedelta as _td_st
                _cut = (datetime.now(timezone.utc) - _td_st(minutes=30)).isoformat()
                _peers = await db.trades.find({
                    "user_id": user_id, "origin": "auto",
                    "status": {"$in": ["open", "pending"]},
                    "action": signal["action"],
                    "opened_at": {"$gte": _cut},
                }).to_list(length=10)
                _blocked = None
                for _pt in _peers:
                    if str(_pt.get("account_id")) == str(cfg_account_id):
                        continue
                    if _bs_st(_pt.get("symbol") or "") != _bs_st(sym):
                        continue
                    _pe, _ps = float(_pt.get("entry_price") or 0), float(_pt.get("stop_loss") or 0)
                    _mkt = float(signal.get("entry_price") or 0)
                    if not (_pe and _ps and _mkt):
                        _blocked = "peer fade open, progress unknown"
                        break
                    _r = (_mkt - _pe) if signal["action"] == "BUY" else (_pe - _mkt)
                    _r = _r / (abs(_pe - _ps) or 1e-9)
                    if _r < 0.3:
                        _blocked = f"peer fade on ..{str(_pt.get('account_id'))[-4:]} at {_r:+.2f}R (need ≥ +0.3R)"
                        break
                if _blocked:
                    await _record_pulse(db, cfg, symbol=sym,
                        action="SKIP", level="warn",
                        reason=f"Correlation stagger: {_blocked} — not mirroring the same fade yet.")
                    await inc_intel_counter(user_id, "correlation_stagger")
                    continue
            except Exception as e:  # noqa: BLE001
                logger.debug("correlation stagger skipped: %s", e)

        # iter-128 · Geometry integrity net — if ANY overlay left a
        # deterministic-engine order with R:R < 1.5, restore the engine's
        # own geometry before sending.
        try:
            _geo = signal.get("engine_geometry")
            if _geo and signal.get("scope") in _DET_PG:
                _e = float(signal.get("entry_price") or 0)
                _s = float(signal.get("stop_loss") or 0)
                _t = float(signal.get("take_profit") or 0)
                if _e and _s and _t and abs(_t - _e) / (abs(_e - _s) or 1e-9) < 1.5:
                    signal["stop_loss"] = _geo["stop_loss"]
                    signal["take_profit"] = _geo["take_profit"]
                    signal["tp1"], signal["tp2"], signal["tp3"] = _geo["tp1"], _geo["tp2"], _geo["tp3"]
                    signal["geometry_restored"] = True
                    logger.info("Geometry restored sym=%s: overlays degraded R:R below 1.5", sym)
        except Exception as e:  # noqa: BLE001
            logger.debug("geometry restore skipped: %s", e)

        # iter-127b · FINAL hard risk clamp — assume STANDARD contract size
        # (most conservative). Protects against mislabeled account_type
        # metadata (e.g. a standard account stored as 'microcent' made pip
        # value 1000× too small and inflated lots). Worst-case USD at the
        # stop must stay within the trade's risk budget.
        try:
            _slp = signal.get("stop_loss")
            _ep = signal.get("entry_price")
            if _slp and _ep:
                from pip_utils import price_to_pips as _p2p, \
                    pip_value_usd_per_lot as _pvpl
                _pips = _p2p(sym, abs(float(_ep) - float(_slp)))
                _pip_std = _pvpl(sym, "standard")
                _eq = float(target_account.get("equity")
                            or target_account.get("balance") or 0)
                _cap_pct = float(signal.get("risk_pct_cap")
                                 or profile.get("risk_pct") or 1.0)
                if _eq > 0 and _pips > 0 and _pip_std > 0:
                    _max_risk_lot = (_eq * _cap_pct / 100.0) / (_pips * _pip_std)
                    if effective_lot > _max_risk_lot:
                        logger.info(
                            "Std-contract risk clamp acct=%s sym=%s: %.2f → %.2f "
                            "lots (budget %.2f%% of $%.0f, SL %.1f pips)",
                            cfg_account_id or "default", sym, effective_lot,
                            _max_risk_lot, _cap_pct, _eq, _pips)
                        effective_lot = max(round(_max_risk_lot, 2), 0.01)
                        sizing_method = sizing_method + "+std_risk_clamp"
        except Exception as e:  # noqa: BLE001
            logger.debug("std risk clamp skipped: %s", e)

        engine = engine_for_account(target_account)
        from versioning import version_stamp
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
                "scope": signal.get("scope"),
                "trend_ride": signal.get("trend_ride"),
                "versions": version_stamp(signal.get("scope")),
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
                signal=signal,
            )
            continue
        logger.info("Bot auto-execute user=%s acct=%s sym=%s trade=%s",
                    user_id, cfg_account_id or "default", sym, trade_doc.get("id"))
        await db.signals.update_one({"_id": ObjectId(signal_id)}, {"$set": {
            "consumed": True,
            **({"corr_kelly_trim": corr_kelly_info} if corr_kelly_info else {}),
            **({"sector_cap_fit": sector_fit_info} if sector_fit_info else {}),
            **({"adaptive_risk": adaptive_risk_info} if adaptive_risk_info else {}),
        }})
        await _record_pulse(db, cfg, symbol=sym,
            action="EXEC", level="info",
            reason=f"Executed {signal['action']} {sym} {effective_lot} lots @ {signal.get('entry_price')} (conf {signal.get('confidence')}%).",
        )
        # iter-134 · Ledger: approved decisions are permanent audit records too
        from trade_decisions import record_decision
        await record_decision(
            db, user_id=user_id, symbol=sym, status="executed",
            stage="execution",
            reason=f"Executed {signal['action']} {effective_lot} lots @ {signal.get('entry_price')}",
            cfg=cfg, signal=signal,
            execution={"trade_id": str(trade_doc.get("id")),
                       "lot_size": effective_lot,
                       "sizing_method": sizing_method})
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
            # Calendar Intelligence outcome learning (iter-107) — classify how
            # the market ACTUALLY reacted to passed high-impact events
            # (breakout/fakeout/reversal/continuation). Self-throttled 1/30min.
            try:
                from calendar_intel import sweep_event_outcomes
                await sweep_event_outcomes(db)
            except Exception as e:
                logger.exception("Calendar intel outcome sweep failed: %s", e)
            # Online Learning (iter-109) — continuous retraining: ML ensemble,
            # RL policy and Bayes retrain after every few closed trades or
            # hourly with fresh data. Self-throttled to 1 sweep/10min.
            try:
                from online_learning import sweep_online_learning
                n_retrained = await sweep_online_learning(db)
                if n_retrained:
                    logger.info("Online learning retrained %d user(s)", n_retrained)
            except Exception as e:
                logger.exception("Online learning sweep failed: %s", e)
            # Self-Evaluation Agent (iter-113) — grade closed trades ("why
            # was I wrong?"), store lessons, learn behavior adjustments.
            try:
                from self_evaluation import sweep_self_evaluation
                await sweep_self_evaluation(db)
            except Exception as e:
                logger.exception("Self-evaluation sweep failed: %s", e)
            # Friday Flat guard (iter-52) — close/tighten open positions ahead
            # of the Friday 21:00 UTC weekly close (weekend gap protection).
            try:
                ffs = await sweep_friday_flat(db)
                if ffs.get("closed") or ffs.get("tightened"):
                    logger.warning("Friday Flat sweep · closed=%d tightened=%d",
                                   ffs["closed"], ffs["tightened"])
            except Exception as e:
                logger.exception("Friday Flat sweep failed: %s", e)
            # Auto Loss Review (iter-54) — aggregate loss analysis with
            # shadow-tested counter-measures (self-throttled to 1 check/10min).
            try:
                from loss_advisor import sweep_loss_reviews
                produced = await sweep_loss_reviews(db)
                if produced:
                    logger.warning("Loss advisor produced %d review(s)", produced)
            except Exception as e:
                logger.exception("Loss advisor sweep failed: %s", e)
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
