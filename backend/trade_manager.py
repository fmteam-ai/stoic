"""Trade Manager — Profit Protection Suite

Runs every TRADE_MANAGER_INTERVAL_SEC (default 15s) and decides whether to:
  1. Move SL to break-even after price moves +N R-multiples
  2. Take partial profit (close X% of lot) at TP1
  3. Trail stop-loss behind price after trailing trigger
  4. Trip the daily drawdown circuit breaker

Modifications are written to the trade doc as a `pending_modification` block.
The MT5 EA picks them up via /api/bridge/poll-trades and applies on its next tick,
then reports back via /api/bridge/report to clear the pending_modification flag.
"""
import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from bson import ObjectId

from database import get_db
from market import get_quote
from ws_manager import manager as ws_manager

logger = logging.getLogger("trade-manager")


def _interval() -> int:
    return int(os.environ.get("TRADE_MANAGER_INTERVAL_SEC", "15"))


def _r_distance(entry: float, stop: float) -> float:
    """Initial R distance (absolute) used to compute R-multiples."""
    return abs(entry - stop)


def _r_multiple(entry: float, current: float, stop: float, action: str) -> float:
    """How many R has price moved in favor of the trade."""
    r = _r_distance(entry, stop)
    if r <= 0:
        return 0.0
    if action == "BUY":
        return (current - entry) / r
    return (entry - current) / r


async def _manage_one_trade(trade: dict, cfg: dict) -> None:
    """Decide if a single open trade needs a modification, write it to DB."""
    db = get_db()
    trade_id = trade["_id"]
    symbol = trade["symbol"]
    action = trade["action"]
    entry = float(trade.get("entry_price") or 0)
    original_sl = float(trade.get("original_stop_loss") or trade.get("stop_loss") or 0)
    current_sl = float(trade.get("stop_loss") or 0)
    original_lot = float(trade.get("original_lot_size") or trade.get("lot_size") or 0)
    if entry <= 0 or original_sl <= 0 or original_lot <= 0:
        return
    if trade.get("pending_modification"):
        return  # waiting for EA to apply previous modification

    # Skip paper trades — they're managed by execution.settle_paper_trades_against_price
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

    r_mult = _r_multiple(entry, current, original_sl, action)

    # 1. Partial close at TP1 — close X% of lot once 1R hit
    if (cfg.get("partial_close_enabled", True)
            and not trade.get("partial_closed", False)
            and r_mult >= cfg.get("partial_close_trigger_r", 1.0)):
        fraction = float(cfg.get("partial_close_fraction", 0.5))
        new_lot = round(original_lot * (1 - fraction), 2)
        if new_lot >= 0.01:
            await db.trades.update_one(
                {"_id": trade_id},
                {"$set": {
                    "pending_modification": {
                        "type": "PARTIAL_CLOSE",
                        "new_volume": new_lot,
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                    },
                    "partial_close_target_lot": new_lot,
                }},
            )
            await ws_manager.broadcast(trade["user_id"], "trade_management", {
                "trade_id": str(trade_id),
                "action": "PARTIAL_CLOSE",
                "from_lot": original_lot,
                "to_lot": new_lot,
                "r_multiple": round(r_mult, 2),
            })
            return  # Don't stack modifications

    # 2. Break-even SL shift — once 1R hit, move SL to entry
    if (cfg.get("breakeven_enabled", True)
            and not trade.get("breakeven_set", False)
            and r_mult >= cfg.get("breakeven_trigger_r", 1.0)):
        be_buffer = 0.0  # exactly at entry; could add small buffer for commission
        new_sl = entry + be_buffer if action == "BUY" else entry - be_buffer
        await db.trades.update_one(
            {"_id": trade_id},
            {"$set": {
                "pending_modification": {
                    "type": "MODIFY_SL",
                    "new_sl": round(new_sl, 5),
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                },
            }},
        )
        await ws_manager.broadcast(trade["user_id"], "trade_management", {
            "trade_id": str(trade_id),
            "action": "BREAKEVEN",
            "new_sl": round(new_sl, 5),
            "r_multiple": round(r_mult, 2),
        })
        return

    # 3. Trailing stop — once trailing_start_r, trail at trailing_distance_r behind price
    if (cfg.get("trailing_enabled", True)
            and r_mult >= cfg.get("trailing_start_r", 1.5)):
        r_dist = _r_distance(entry, original_sl)
        trail_dist = r_dist * float(cfg.get("trailing_distance_r", 0.7))
        new_sl = (current - trail_dist) if action == "BUY" else (current + trail_dist)
        # Only update if new SL is strictly better than current
        is_better = (new_sl > current_sl) if action == "BUY" else (new_sl < current_sl)
        # Step gate: don't churn — only trail in 0.2R steps
        step_threshold = r_dist * 0.2
        sl_delta_ok = abs(new_sl - current_sl) >= step_threshold
        if is_better and sl_delta_ok:
            await db.trades.update_one(
                {"_id": trade_id},
                {"$set": {
                    "pending_modification": {
                        "type": "MODIFY_SL",
                        "new_sl": round(new_sl, 5),
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                    },
                    "trail_active": True,
                }},
            )
            await ws_manager.broadcast(trade["user_id"], "trade_management", {
                "trade_id": str(trade_id),
                "action": "TRAIL",
                "new_sl": round(new_sl, 5),
                "r_multiple": round(r_mult, 2),
            })


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
        # Trip
        await db.bot_configs.update_one(
            {"user_id": user_id},
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
