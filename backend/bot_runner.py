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
    """Return list of accounts whose EA has sent a heartbeat in the last 5 min."""
    cursor = db.accounts.find({"user_id": user_id})
    accs = await cursor.to_list(length=50)
    fresh = []
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    for a in accs:
        hb = a.get("last_heartbeat") or ""
        if hb and hb >= cutoff:
            fresh.append(a)
    return fresh


async def _process_user(db, cfg: dict):
    user_id = cfg["user_id"]
    symbols = cfg.get("symbols") or []
    if not symbols:
        return

    all_accounts = await db.accounts.find({"user_id": user_id}).to_list(length=50)

    # 1. Circuit breaker check first — never analyse if tripped
    cb = await check_and_trip(db, user_id, cfg, all_accounts)
    if cb["tripped"]:
        await ws_manager.broadcast(user_id, "circuit_breaker_tripped", cb)
        logger.warning("Circuit breaker tripped for user=%s: %s", user_id, cb["reason"])
        return

    connected = [a for a in all_accounts
                 if a.get("last_heartbeat") and
                 a["last_heartbeat"] >= (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()]

    risk_level = cfg.get("risk_level", "medium")
    auto_exec = bool(cfg.get("auto_execute", True))
    max_concurrent = int(cfg.get("max_concurrent_trades", 3))

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

        # Auto-execute decision
        if not (auto_exec and signal["tradeable"]):
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
        trade_doc = {
            "user_id": user_id,
            "account_id": str(target_account["_id"]),
            "signal_id": signal_id,
            "symbol": signal["symbol"],
            "action": signal["action"],
            "lot_size": signal["lot_size"],
            "entry_price": signal["entry_price"],
            "stop_loss": signal["stop_loss"],
            "take_profit": signal["take_profit"],
            "exit_price": None,
            "pnl": 0.0,
            "status": "pending",
            "mt5_ticket": None,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "closed_at": None,
            "error": None,
            "origin": "auto",
        }
        tr = await db.trades.insert_one(trade_doc)
        await db.signals.update_one({"_id": ObjectId(signal_id)}, {"$set": {"consumed": True}})
        trade_doc["id"] = str(tr.inserted_id)
        trade_doc.pop("_id", None)
        await ws_manager.broadcast(user_id, "trade_created", trade_doc)
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
        except Exception as e:
            logger.exception("Bot runner tick failed: %s", e)
        await asyncio.sleep(interval)
