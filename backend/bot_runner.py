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
from ai_signals import analyze_symbol
from circuit_breakers import check_and_trip
from ws_manager import manager as ws_manager
from rate_limiter import check_and_record as rl_check
from execution import for_account as engine_for_account
from execution import settle_paper_trades_against_price
from trigger_sweeper import sweep_once as sweep_triggers
from subscription_service import is_active as subscription_active
from auto_tune import get_auto_threshold

logger = logging.getLogger("bot-runner")

# Internal cooldown tracker { (user_id, symbol): datetime_next_eligible }
_next_signal_at: dict = {}


def _loop_interval() -> int:
    return int(os.environ.get("BOT_LOOP_INTERVAL_SEC", "60"))


def _cooldown_minutes() -> int:
    return int(os.environ.get("BOT_SIGNAL_COOLDOWN_MIN", "15"))


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

    # Count current open + pending trades to respect max_concurrent
    inflight = await db.trades.count_documents({
        "user_id": user_id,
        "status": {"$in": ["pending", "open"]},
    })

    for sym in symbols:
        if _on_cooldown(user_id, sym):
            continue

        try:
            signal = await analyze_symbol(sym, risk_level)
        except Exception as e:
            logger.exception("analyze_symbol failed user=%s sym=%s: %s", user_id, sym, e)
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
        except Exception as e:
            logger.exception("Bot runner tick failed: %s", e)
        await asyncio.sleep(interval)
