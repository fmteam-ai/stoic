"""Trade Manager — Profit Protection Suite (pip-based, 3-tier partial close)

Runs every TRADE_MANAGER_INTERVAL_SEC (default 15s) and for each open trade:
  1. At +100 pips profit  → close 50% AND move SL to entry (break-even)
  2. At +200 pips profit  → close another 25% (now 25% remains)
  3. At +300 pips profit  → close the last 25%
  4. Tracks daily realised drawdown and trips the circuit breaker if breached

Modifications are written to the trade doc as a `pending_modification` block.
The MT5 EA picks them up via /api/bridge/poll-trades and applies on its next tick,
then reports back via /api/bridge/modification-ack to clear the flag.
"""
import os
import asyncio
import logging
from datetime import datetime, timezone
from bson import ObjectId

from database import get_db
from market import get_quote
from ws_manager import manager as ws_manager
from pip_utils import price_to_pips
from notifier import (
    notify_breakeven, notify_partial_close, notify_trail, notify_circuit_breaker
)

logger = logging.getLogger("trade-manager")

# Default pip targets (override per-trade if signal provided them)
DEFAULT_SL_PIPS = 150
DEFAULT_TP_PIPS = (100, 200, 300)   # (TP1, TP2, TP3)
DEFAULT_BE_TRIGGER_PIPS = 100        # move SL to entry at +100 pips


def _interval() -> int:
    return int(os.environ.get("TRADE_MANAGER_INTERVAL_SEC", "15"))


def _pips_in_profit(entry: float, current: float, action: str, symbol: str) -> float:
    """How many pips price has moved in favour of the trade."""
    diff = (current - entry) if action == "BUY" else (entry - current)
    return price_to_pips(symbol, diff)


def _trade_targets(trade: dict) -> tuple:
    """Resolve (sl_pips, tp1_pips, tp2_pips, tp3_pips) for a trade.

    Uses values stored on the trade doc; falls back to module defaults.
    """
    sl_pips = trade.get("sl_pips") or DEFAULT_SL_PIPS
    tp_pips = trade.get("tp_pips") or list(DEFAULT_TP_PIPS)
    tp1, tp2, tp3 = (tp_pips + [None, None, None])[:3]
    tp1 = tp1 or DEFAULT_TP_PIPS[0]
    tp2 = tp2 or DEFAULT_TP_PIPS[1]
    tp3 = tp3 or DEFAULT_TP_PIPS[2]
    return float(sl_pips), float(tp1), float(tp2), float(tp3)


async def _manage_one_trade(trade: dict, cfg: dict) -> None:
    """Decide if a single open trade needs a modification, write it to DB."""
    db = get_db()
    trade_id = trade["_id"]
    symbol = trade["symbol"]
    action = trade["action"]
    entry = float(trade.get("entry_price") or 0)
    original_lot = float(trade.get("original_lot_size") or trade.get("lot_size") or 0)
    current_lot = float(trade.get("lot_size") or 0)
    if entry <= 0 or original_lot <= 0:
        return
    if trade.get("pending_modification"):
        return  # waiting for EA to apply previous modification

    # Skip paper trades — they're settled by execution.settle_paper_trades_against_price
    mode = (trade.get("mode") or "live").lower()
    if mode == "paper":
        return
    if not trade.get("mt5_ticket"):
        return  # still pending, EA hasn't filled yet

    try:
        quote = await get_quote(symbol)
        current = float(quote.get("price") or 0)
    except Exception as e:
        logger.warning("price fetch failed for %s: %s", symbol, e)
        return
    if current <= 0:
        return

    _, tp1_pips, tp2_pips, tp3_pips = _trade_targets(trade)
    pips_up = _pips_in_profit(entry, current, action, symbol)

    # Tier 3 — close remaining at +tp3 pips
    if (not trade.get("tp3_closed")
            and trade.get("tp2_closed")
            and pips_up >= tp3_pips):
        new_lot = 0.0  # Full close requested via close_requested flag
        await db.trades.update_one(
            {"_id": trade_id},
            {"$set": {
                "close_requested": True,
                "tp3_closed": True,
                "pending_modification": {
                    "type": "FULL_CLOSE",
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                },
            }},
        )
        await ws_manager.broadcast(trade["user_id"], "trade_management", {
            "trade_id": str(trade_id),
            "action": "FULL_CLOSE_TP3",
            "from_lot": current_lot,
            "to_lot": new_lot,
            "pips": round(pips_up, 1),
        })
        try:
            await notify_partial_close(trade["user_id"], str(trade_id), current_lot, new_lot, pips_up / max(1, tp1_pips))
        except Exception:
            pass
        return

    # Tier 2 — close another 25% at +tp2 pips (remaining = 25% of original)
    if (not trade.get("tp2_closed")
            and trade.get("tp1_closed")
            and pips_up >= tp2_pips):
        new_lot = round(original_lot * 0.25, 2)
        if new_lot >= 0.01:
            await db.trades.update_one(
                {"_id": trade_id},
                {"$set": {
                    "pending_modification": {
                        "type": "PARTIAL_CLOSE",
                        "new_volume": new_lot,
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                    },
                    "tp2_target_lot": new_lot,
                }},
            )
            await ws_manager.broadcast(trade["user_id"], "trade_management", {
                "trade_id": str(trade_id),
                "action": "PARTIAL_CLOSE_TP2",
                "from_lot": current_lot,
                "to_lot": new_lot,
                "pips": round(pips_up, 1),
            })
            try:
                await notify_partial_close(trade["user_id"], str(trade_id), current_lot, new_lot, pips_up / max(1, tp1_pips))
            except Exception:
                pass
            return

    # Tier 1 — close 50% AND move SL to entry at +tp1 pips
    if not trade.get("tp1_closed") and pips_up >= tp1_pips:
        new_lot = round(original_lot * 0.5, 2)
        if new_lot >= 0.01:
            await db.trades.update_one(
                {"_id": trade_id},
                {"$set": {
                    "pending_modification": {
                        "type": "PARTIAL_CLOSE",
                        "new_volume": new_lot,
                        "new_sl": round(entry, 5),  # also move SL to entry on the EA modification
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                    },
                    "tp1_target_lot": new_lot,
                    "be_target_sl": round(entry, 5),
                }},
            )
            await ws_manager.broadcast(trade["user_id"], "trade_management", {
                "trade_id": str(trade_id),
                "action": "PARTIAL_CLOSE_TP1_AND_BE",
                "from_lot": current_lot,
                "to_lot": new_lot,
                "new_sl": round(entry, 5),
                "pips": round(pips_up, 1),
            })
            try:
                await notify_partial_close(trade["user_id"], str(trade_id), current_lot, new_lot, 1.0)
                await notify_breakeven(trade["user_id"], str(trade_id), round(entry, 5), 1.0)
            except Exception:
                pass
            return


async def _check_daily_drawdown(user_id: str, cfg: dict) -> None:
    """If today's realised P&L falls below -daily_drawdown_pct of starting equity,
    auto-stop the bot for this user and trip the circuit breaker."""
    if not cfg.get("daily_drawdown_enabled", True):
        return
    if not cfg.get("active"):
        return
    db = get_db()

    today_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_iso = today_utc.isoformat()

    # Sum today's realised P&L across all of this user's accounts
    cursor = db.trades.find({
        "user_id": user_id,
        "status": "closed",
        "closed_at": {"$gte": today_iso},
    })
    closed = await cursor.to_list(length=500)
    today_pnl = sum(float(t.get("pnl") or 0) for t in closed)

    # Sum starting equity from all user's accounts
    accts = await db.accounts.find({"user_id": user_id}).to_list(length=20)
    equity = sum(float(a.get("balance") or a.get("initial_balance") or 0) for a in accts) or 0
    if equity <= 0:
        return

    limit_pct = float(cfg.get("daily_drawdown_pct", 3.0))
    drawdown_pct = (today_pnl / equity) * 100.0
    if drawdown_pct <= -limit_pct:
        # Trip — disable every active bot_config for this user (default + all
        # per-account overrides). Drawdown is user-level so all bots stop.
        await db.bot_configs.update_many(
            {"user_id": user_id, "active": True},
            {"$set": {
                "active": False,
                "circuit_breaker_tripped_at": datetime.now(timezone.utc).isoformat(),
                "circuit_breaker_reason": (
                    f"Daily drawdown {drawdown_pct:.2f}% breached -{limit_pct:.2f}% limit"
                ),
            }},
        )
        await ws_manager.broadcast(user_id, "circuit_breaker_tripped", {
            "reason": f"Daily drawdown {drawdown_pct:.2f}% breached -{limit_pct:.2f}% limit",
            "today_pnl": round(today_pnl, 2),
            "equity": round(equity, 2),
        })
        try:
            await notify_circuit_breaker(
                user_id,
                f"Daily drawdown {drawdown_pct:.2f}% breached -{limit_pct:.2f}% limit",
                round(today_pnl, 2),
                round(equity, 2),
            )
        except Exception:
            pass
        logger.warning("circuit breaker tripped for user=%s drawdown=%.2f%%", user_id, drawdown_pct)


async def _tick() -> None:
    """One pass of the trade manager loop."""
    db = get_db()
    # Collect all unique active user_ids with at least one open trade
    open_trades = await db.trades.find({
        "status": "open",
        "mt5_ticket": {"$ne": None},
    }).to_list(length=200)

    # Daily drawdown check — group by user
    user_ids = set(t["user_id"] for t in open_trades)
    # Also include users with active bot but no open trades (to catch DD from closed trades)
    active_cfgs = await db.bot_configs.find({"active": True}).to_list(length=200)
    user_ids.update(c["user_id"] for c in active_cfgs)

    cfg_by_user = {c["user_id"]: c for c in active_cfgs}
    # Fetch missing configs
    for uid in user_ids:
        if uid not in cfg_by_user:
            c = await db.bot_configs.find_one({"user_id": uid})
            if c:
                cfg_by_user[uid] = c

    for uid in user_ids:
        cfg = cfg_by_user.get(uid)
        if not cfg:
            continue
        try:
            await _check_daily_drawdown(uid, cfg)
        except Exception as e:
            logger.exception("daily drawdown check failed for user=%s: %s", uid, e)

    # Per-trade management
    for t in open_trades:
        cfg = cfg_by_user.get(t["user_id"])
        if not cfg:
            continue
        try:
            await _manage_one_trade(t, cfg)
        except Exception as e:
            logger.exception("trade management failed trade_id=%s: %s", t.get("_id"), e)


async def run_loop() -> None:
    """Forever loop — started from server.py startup."""
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.exception("trade_manager tick crashed: %s", e)
        await asyncio.sleep(_interval())
