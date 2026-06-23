from datetime import datetime, timezone
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId
from pydantic import BaseModel, Field

from auth import get_current_user
from database import get_db
from market import get_quote
from rate_limiter import check_and_record
from execution import for_account as engine_for_account

router = APIRouter(prefix="/trades", tags=["trades"])


class ManualTradeRequest(BaseModel):
    account_id: str
    symbol: str
    action: Literal["BUY", "SELL"]
    lot_size: float = Field(0.01, gt=0, le=100)
    sl_pips: float = Field(150, gt=0, le=10000)
    tp1_pips: float = Field(100, gt=0, le=10000)
    tp2_pips: float = Field(200, gt=0, le=10000)
    tp3_pips: float = Field(300, gt=0, le=10000)


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc


@router.get("")
async def list_trades(limit: int = 100, status: str = None, user=Depends(get_current_user)):
    db = get_db()
    query = {"user_id": user["id"]}
    if status:
        query["status"] = status
    cursor = db.trades.find(query).sort("opened_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return [_serialize(d) for d in docs]


def _aggregate_stats(closed: list) -> dict:
    """Compute aggregate P&L stats from a list of closed trade docs."""
    total = len(closed)
    wins = [t for t in closed if (t.get("pnl") or 0) > 0]
    losses = [t for t in closed if (t.get("pnl") or 0) < 0]
    total_pnl = sum((t.get("pnl") or 0) for t in closed)
    win_rate = (len(wins) / total * 100) if total else 0
    avg_win = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0
    avg_loss = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0
    return {
        "total_trades": total,
        "win_rate": round(win_rate, 2),
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "wins": len(wins),
        "losses": len(losses),
    }


@router.get("/stats")
async def trade_stats(user=Depends(get_current_user)):
    db = get_db()
    closed = await db.trades.find(
        {"user_id": user["id"], "status": "closed"}
    ).to_list(length=1000)
    open_trades = await db.trades.find(
        {"user_id": user["id"], "status": "open"}
    ).to_list(length=100)
    return {**_aggregate_stats(closed), "open_trades": len(open_trades)}


@router.get("/live")
async def live_open_trades(user=Depends(get_current_user)):
    """Open trades enriched with current price, unrealised P&L, distance to SL/TP1/TP2/TP3.

    Used by the Dashboard's "Time-to-Target" widget. Refreshes every ~5s on the client.
    """
    from pip_utils import pip_size, price_to_pips
    db = get_db()
    cursor = db.trades.find({
        "user_id": user["id"], "status": {"$in": ["pending", "open"]}
    }).sort("opened_at", -1)
    trades = await cursor.to_list(length=50)
    if not trades:
        return []

    # Fetch latest quote per unique symbol once
    symbols = sorted({t["symbol"] for t in trades})
    prices = {}
    for sym in symbols:
        try:
            q = await get_quote(sym)
            prices[sym] = float(q.get("price") or 0)
        except Exception:
            prices[sym] = 0.0

    out = []
    for t in trades:
        sym = t["symbol"]
        action = t["action"]
        entry = float(t.get("entry_price") or 0)
        sl = float(t.get("stop_loss") or 0)
        tp1 = float(t.get("tp1") or 0)
        tp2 = float(t.get("tp2") or 0)
        tp3 = float(t.get("tp3") or t.get("take_profit") or 0)
        lot = float(t.get("lot_size") or 0)
        current = prices.get(sym, 0.0)

        def pip_diff_to_target(target):
            return price_to_pips(sym, abs(target - current)) if target and current else None
        pips_in_profit = None
        if current and entry:
            diff = (current - entry) if action == "BUY" else (entry - current)
            pips_in_profit = price_to_pips(sym, diff)

        # Unrealised P&L estimate (USD). For accuracy this should use contract size,
        # but for display we approximate: BTC pip = $1 per lot, gold pip = $1 per 0.01 lot.
        pip_val_per_lot = 10.0 if pip_size(sym) == 0.0001 else (
            1.0 if sym.upper() in ("BTCUSD", "ETHUSD") else 10.0
        )
        unrealised_pnl = None
        if pips_in_profit is not None:
            unrealised_pnl = round(pips_in_profit * pip_val_per_lot * lot, 2)

        # Distance to each target as percentage of total entry-to-target distance
        def _pct(target):
            if not (current and entry and target):
                return None
            full = abs(target - entry)
            covered = abs(current - entry) if (
                (action == "BUY" and current >= entry) or (action == "SELL" and current <= entry)
            ) else 0
            return round(min(100, (covered / full) * 100), 1) if full > 0 else None

        out.append({
            "id": str(t["_id"]),
            "symbol": sym,
            "action": action,
            "lot_size": lot,
            "entry_price": entry,
            "stop_loss": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "current_price": current,
            "pips_in_profit": round(pips_in_profit, 1) if pips_in_profit is not None else None,
            "unrealised_pnl": unrealised_pnl,
            "pips_to_sl": round(pip_diff_to_target(sl) or 0, 1) if sl else None,
            "pips_to_tp1": round(pip_diff_to_target(tp1) or 0, 1) if tp1 else None,
            "pips_to_tp2": round(pip_diff_to_target(tp2) or 0, 1) if tp2 else None,
            "pips_to_tp3": round(pip_diff_to_target(tp3) or 0, 1) if tp3 else None,
            "progress_to_tp1_pct": _pct(tp1),
            "progress_to_tp2_pct": _pct(tp2),
            "progress_to_tp3_pct": _pct(tp3),
            "tp1_closed": bool(t.get("tp1_closed")),
            "tp2_closed": bool(t.get("tp2_closed")),
            "tp3_closed": bool(t.get("tp3_closed")),
            "breakeven_set": bool(t.get("breakeven_set")),
            "status": t.get("status"),
            "mode": t.get("mode"),
            "opened_at": t.get("opened_at"),
        })
    return out


@router.post("/execute/{signal_id}")
async def execute_signal(signal_id: str, payload: dict, user=Depends(get_current_user)):
    """Queue a trade for execution via the MT5 EA bridge.

    Body: {"account_id": "..."}
    The trade is created in 'pending' status; the EA polls /bridge/poll-trades
    and reports back via /bridge/report.
    """
    account_id = payload.get("account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id required")

    db = get_db()
    signal = await db.signals.find_one({"_id": ObjectId(signal_id), "user_id": user["id"]})
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")
    if signal.get("action") == "HOLD":
        raise HTTPException(status_code=400, detail="Cannot execute HOLD signal")

    account = await db.accounts.find_one({"_id": ObjectId(account_id), "user_id": user["id"]})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # Kill-switch: hard cap on orders per minute per user
    rl = check_and_record(user["id"])
    if not rl["allowed"]:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: max {rl['limit']} orders/min reached. Retry in {rl['retry_in_s']}s.",
        )

    # Execution Factory — paper vs live engine
    engine = engine_for_account(account)
    trade_doc = await engine.execute(
        user_id=user["id"],
        account=account,
        signal={
            "signal_id": signal_id,
            "symbol": signal["symbol"],
            "action": signal["action"],
            "lot_size": signal.get("lot_size", 0.01),
            "entry_price": signal.get("entry_price", 0),
            "stop_loss": signal.get("stop_loss", 0),
            "take_profit": signal.get("take_profit", 0),
            "origin": "manual",
        },
    )
    await db.signals.update_one(
        {"_id": ObjectId(signal_id), "user_id": user["id"]},
        {"$set": {"consumed": True}},
    )
    return trade_doc


@router.post("/manual")
async def execute_manual_trade(payload: ManualTradeRequest, user=Depends(get_current_user)):
    """Place a manual paper trade — bypasses AI signal/confidence gating.

    Allowed ONLY for paper-mode accounts; live accounts must execute via AI signals
    so that the EA bridge + risk vetoes apply.
    """
    db = get_db()
    account = await db.accounts.find_one({"_id": ObjectId(payload.account_id), "user_id": user["id"]})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if (account.get("mode") or "live").lower() != "paper":
        raise HTTPException(status_code=400, detail="Manual trades are only allowed on paper accounts")

    rl = check_and_record(user["id"])
    if not rl["allowed"]:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: max {rl['limit']} orders/min reached. Retry in {rl['retry_in_s']}s.",
        )

    quote = await get_quote(payload.symbol)
    price = quote.get("price")
    if not price or price <= 0:
        raise HTTPException(status_code=502, detail=f"Could not get live quote for {payload.symbol}")

    from pip_utils import pips_to_price
    sl_dist = pips_to_price(payload.symbol, payload.sl_pips)
    tp1_dist = pips_to_price(payload.symbol, payload.tp1_pips)
    tp2_dist = pips_to_price(payload.symbol, payload.tp2_pips)
    tp3_dist = pips_to_price(payload.symbol, payload.tp3_pips)
    if payload.action == "BUY":
        stop_loss = round(price - sl_dist, 5)
        tp1 = round(price + tp1_dist, 5)
        tp2 = round(price + tp2_dist, 5)
        tp3 = round(price + tp3_dist, 5)
    else:
        stop_loss = round(price + sl_dist, 5)
        tp1 = round(price - tp1_dist, 5)
        tp2 = round(price - tp2_dist, 5)
        tp3 = round(price - tp3_dist, 5)

    engine = engine_for_account(account)
    trade_doc = await engine.execute(
        user_id=user["id"],
        account=account,
        signal={
            "signal_id": None,
            "symbol": payload.symbol,
            "action": payload.action,
            "lot_size": payload.lot_size,
            "entry_price": price,
            "stop_loss": stop_loss,
            "take_profit": tp3,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "sl_pips": payload.sl_pips,
            "tp_pips": [payload.tp1_pips, payload.tp2_pips, payload.tp3_pips],
            "origin": "manual_test",
        },
    )
    return trade_doc


@router.post("/{trade_id}/close")
async def close_trade(trade_id: str, user=Depends(get_current_user)):
    """Mark a trade as pending-close so the EA closes it on next poll."""
    db = get_db()
    result = await db.trades.update_one(
        {"_id": ObjectId(trade_id), "user_id": user["id"], "status": "open"},
        {"$set": {"status": "pending", "close_requested": True, "close_reason": "manual"}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Open trade not found")
    return {"ok": True}
