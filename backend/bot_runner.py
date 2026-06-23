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

logger = logging.getLogger("bot-runner")

# Internal cooldown tracker { (user_id, symbol): datetime_next_eligible }
_next_signal_at: dict = {}


def _loop_interval() -> int:
    return int(os.environ.get("BOT_LOOP_INTERVAL_SEC", "60"))


def _cooldown_minutes() -> int:
    return int(os.environ.get("BOT_SIGNAL_COOLDOWN_MIN", "15"))


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


def _mark_cooldown(user_id: str, symbol: str):
    _next_signal_at[(user_id, symbol)] = (
        datetime.now(timezone.utc) + timedelta(minutes=_cooldown_minutes())
    )


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


async def _process_user(db, cfg: dict):
    user_id = cfg["user_id"]
    symbols = cfg.get("symbols") or []
    if not symbols:
        return

    # 0. Subscription gate — paper accounts always allowed; live execution requires active sub
    entitlement = await subscription_active(user_id)

    all_accounts = await db.accounts.find({"user_id": user_id}).to_list(length=50)

    # 1. Circuit breaker check first — never analyse if tripped
    cb = await check_and_trip(db, user_id, cfg, all_accounts)
    if cb["tripped"]:
        await ws_manager.broadcast(user_id, "circuit_breaker_tripped", cb)
        logger.warning("Circuit breaker tripped for user=%s: %s", user_id, cb["reason"])
        return

    # Paper-mode-only fallback when subscription is inactive
    if not entitlement["active"]:
        all_accounts = [a for a in all_accounts if (a.get("mode") or "live") == "paper"]
        if not all_accounts:
            # No paper account either → skip silently. UI banner will prompt to subscribe.
            return

    connected = [a for a in all_accounts
                 if (a.get("mode") or "live") == "paper"
                 or (a.get("last_heartbeat") and
                     a["last_heartbeat"] >= (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())]

    risk_level = cfg.get("risk_level", "medium")
    auto_exec = bool(cfg.get("auto_execute", True))
    max_concurrent = int(cfg.get("max_concurrent_trades", 3))
    auto_tune_enabled = bool(cfg.get("auto_tune_enabled", True))
    spread_filter_enabled = bool(cfg.get("spread_filter_enabled", False))
    max_spread_pips = cfg.get("max_spread_pips") or {}
    anti_tilt_enabled = bool(cfg.get("anti_tilt_enabled", True))
    anti_tilt_n = int(cfg.get("anti_tilt_consecutive_losses", 3))
    anti_tilt_hours = int(cfg.get("anti_tilt_freeze_hours", 4))
    trade_of_day_cap = int(cfg.get("trade_of_day_cap", 1) or 0)
    asia_skip_xau = bool(cfg.get("asia_session_skip_xau", True))
    sl_cooldown_enabled = bool(cfg.get("sl_cooldown_enabled", True))
    sl_cooldown_min = int(cfg.get("sl_cooldown_minutes", _sl_cooldown_minutes_default()) or 0)

    # === Anti-tilt: freeze auto-execute if last N closed trades all lost ===
    anti_tilt_active = False
    if anti_tilt_enabled and anti_tilt_n > 0:
        recent = await db.trades.find(
            {"user_id": user_id, "status": "closed"}
        ).sort("closed_at", -1).limit(anti_tilt_n).to_list(length=anti_tilt_n)
        if len(recent) == anti_tilt_n and all(float(r.get("pnl") or 0) <= 0 for r in recent):
            last_close = recent[0].get("closed_at")
            try:
                lc = datetime.fromisoformat(str(last_close).replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - lc < timedelta(hours=anti_tilt_hours):
                    anti_tilt_active = True
                    logger.warning("Anti-tilt active for user=%s — last %d trades lost, freezing auto-exec",
                                   user_id, anti_tilt_n)
            except Exception:
                pass

    # Count current open + pending trades to respect max_concurrent
    inflight = await db.trades.count_documents({
        "user_id": user_id,
        "status": {"$in": ["pending", "open"]},
    })

    # If anti-tilt fired above, freeze ALL new entries for this cycle.
    # Existing open trades are NOT closed — only new entries are blocked.
    if anti_tilt_active:
        return

    for sym in symbols:
        if _on_cooldown(user_id, sym):
            continue

        # Capital-preservation guards (skip BEFORE expensive AI analysis)
        # 1. Asia-session skip for XAU (00:00-07:00 UTC = chop graveyard for gold)
        if asia_skip_xau and sym.upper() == "XAUUSD":
            now_h = datetime.now(timezone.utc).hour
            if now_h < 7:
                logger.info("Asia-session skip user=%s sym=%s hour=%d", user_id, sym, now_h)
                continue

        # 2. Trade-of-the-day cap: max N new trades per symbol per UTC day
        if trade_of_day_cap > 0:
            day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            today_count = await db.trades.count_documents({
                "user_id": user_id,
                "symbol": sym,
                "created_at": {"$gte": day_start.isoformat()},
            })
            if today_count >= trade_of_day_cap:
                logger.info("Trade-of-day cap reached user=%s sym=%s count=%d/%d",
                            user_id, sym, today_count, trade_of_day_cap)
                continue

        # 3. Per-symbol SL cooldown — if last trade on this symbol stopped out
        #    within `sl_cooldown_minutes`, block re-entry. Prevents revenge-regime
        #    bounce-trading into the same losing setup.
        if sl_cooldown_enabled and sl_cooldown_min > 0:
            sl_cd = await _on_sl_cooldown(db, user_id, sym, sl_cooldown_min)
            if sl_cd:
                logger.info(
                    "SL cooldown user=%s sym=%s last_sl=%smin ago, resumes in %smin",
                    user_id, sym, sl_cd["age_minutes"], sl_cd["resumes_in_min"],
                )
                await inc_intel_counter(user_id, "sl_cooldown_block")
                continue

        try:
            # Route every tick through the multi-agent orchestrator:
            # Research → Strategy → Risk. Returns the post-risk signal and
            # persists the activity log to `agent_activity`.
            orch = get_orchestrator()
            active_positions = await db.trades.find({
                "user_id": user_id,
                "status": {"$in": ["open", "pending"]},
            }).to_list(length=50)
            tick_out = await orch.analyze_tick(
                user_id=user_id, symbol=sym, risk_level=risk_level,
                active_positions=active_positions,
            )
            signal = tick_out.get("signal")
            if not signal:
                continue
        except Exception as e:
            logger.exception("orchestrator.analyze_tick failed user=%s sym=%s: %s", user_id, sym, e)
            continue

        # Auto-Tune: raise the min-confidence threshold using historical analytics
        auto_tune_block_reason = None
        if auto_tune_enabled:
            try:
                tune = await get_auto_threshold(user_id, sym, risk_level)
                signal["auto_tune"] = tune
                eff = float(tune.get("effective_threshold") or 0)
                if (signal.get("confidence") or 0) < eff:
                    auto_tune_block_reason = (
                        f"Auto-tune raised threshold to {eff:.0f}% "
                        f"(source={tune.get('source')}, samples={tune.get('total_samples')}); "
                        f"signal {signal.get('confidence')}% — auto-execute skipped."
                    )
            except Exception as e:
                logger.exception("auto_tune failed user=%s sym=%s: %s", user_id, sym, e)

        # Count Multi-Timeframe vetoes (gate fired inside ai_signals)
        try:
            mtf_gate = signal.get("mtf_gate") or {}
            if mtf_gate.get("checked") and mtf_gate.get("aligned") is False:
                await inc_intel_counter(user_id, "mtf_veto")
            # Count Learned Meta-Classifier vetoes
            lm = signal.get("learned_meta") or {}
            if lm.get("verdict") == "REJECT":
                await inc_intel_counter(user_id, "learned_meta_veto")
            # Count A+ confluence vetoes
            aplus = signal.get("aplus_confluence") or {}
            if aplus.get("checks") and aplus.get("passed") is False:
                await inc_intel_counter(user_id, "aplus_veto")
            # Count R:R vetoes — signal action HOLD but veto_applied True is too broad;
            # use rr_ratio + reasoning fingerprint
            reasoning_blob = signal.get("reasoning") or ""
            if "VETO (R:R)" in reasoning_blob:
                await inc_intel_counter(user_id, "rr_veto")
        except Exception:
            pass

        signal["user_id"] = user_id
        signal["consumed"] = False
        signal["created_at"] = datetime.now(timezone.utc).isoformat()
        signal["origin"] = "auto"
        result = await db.signals.insert_one(signal)
        signal_id = str(result.inserted_id)
        broadcast_payload = {**signal, "id": signal_id}
        broadcast_payload.pop("_id", None)
        await ws_manager.broadcast(user_id, "signal_created", broadcast_payload)
        _mark_cooldown(user_id, sym)

        # Telegram alert on high-confidence non-HOLD signals
        try:
            if signal.get("action") in ("BUY", "SELL") and (signal.get("confidence") or 0) >= 75:
                from notifier import notify_high_conf_signal
                await notify_high_conf_signal(user_id, broadcast_payload)
        except Exception:
            pass

        # Auto-execute decision
        if not (auto_exec and signal["tradeable"]):
            continue
        if auto_tune_block_reason:
            logger.info("Auto-tune block user=%s sym=%s: %s", user_id, sym, auto_tune_block_reason)
            await inc_intel_counter(user_id, "auto_tune_block")
            continue
        if inflight >= max_concurrent:
            logger.info("Max concurrent (%s) reached for user=%s; skipping execute", max_concurrent, user_id)
            continue
        if not connected:
            continue
        # Per-user rate-limit guard — hard cap from env
        rl = rl_check(user_id)
        if not rl["allowed"]:
            logger.warning("Rate-limited user=%s, count_60s=%s/%s, skip auto-execute",
                           user_id, rl["count_60s"], rl["limit"])
            continue
        target_account = connected[0]

        # Spread filter — block auto-execution when MT5 spread > configured cap
        if spread_filter_enabled and (target_account.get("mode") or "live") == "live":
            spreads = target_account.get("current_spreads") or {}
            current_sp = spreads.get(sym)
            cap = max_spread_pips.get(sym)
            if current_sp is not None and cap is not None and current_sp > float(cap):
                logger.info(
                    "Spread filter block user=%s sym=%s spread=%.1fp cap=%.1fp",
                    user_id, sym, current_sp, cap,
                )
                await db.signals.update_one(
                    {"_id": result.inserted_id},
                    {"$set": {"spread_filter_block": {
                        "spread_pips": current_sp,
                        "cap_pips": cap,
                        "account_id": str(target_account["_id"]),
                    }}},
                )
                await inc_intel_counter(user_id, "spread_block")
                continue
        engine = engine_for_account(target_account)
        trade_doc = await engine.execute(
            user_id=user_id,
            account=target_account,
            signal={
                "signal_id": signal_id,
                "symbol": signal["symbol"],
                "action": signal["action"],
                "lot_size": signal["lot_size"],
                "entry_price": signal["entry_price"],
                "stop_loss": signal["stop_loss"],
                "take_profit": signal["take_profit"],
                "origin": "auto",
            },
        )
        logger.info("Bot auto-execute user=%s sym=%s trade=%s", user_id, sym, trade_doc.get("id"))
        await db.signals.update_one({"_id": ObjectId(signal_id)}, {"$set": {"consumed": True}})
        inflight += 1


async def loop():
    """Long-running asyncio task. Iterates over all active bots every N seconds."""
    interval = _loop_interval()
    logger.info("Bot runner started — interval=%ss, cooldown=%smin", interval, _cooldown_minutes())
    while True:
        try:
            db = get_db()
            cursor = db.bot_configs.find({"active": True})
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
        except Exception as e:
            logger.exception("Bot runner tick failed: %s", e)
        await asyncio.sleep(interval)
