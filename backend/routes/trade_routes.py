from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/trades", tags=["trades"])


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

    trade_doc = {
        "user_id": user["id"],
        "account_id": account_id,
        "signal_id": signal_id,
        "symbol": signal["symbol"],
        "action": signal["action"],
        "lot_size": signal.get("lot_size", 0.01),
        "entry_price": signal.get("entry_price", 0),
        "stop_loss": signal.get("stop_loss", 0),
        "take_profit": signal.get("take_profit", 0),
        "exit_price": None,
        "pnl": 0.0,
        "status": "pending",
        "mt5_ticket": None,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "closed_at": None,
        "error": None,
    }
    result = await db.trades.insert_one(trade_doc)
    trade_doc["_id"] = result.inserted_id
    # Mark signal as consumed (re-scope by user_id as defense in depth)
    await db.signals.update_one(
        {"_id": ObjectId(signal_id), "user_id": user["id"]},
        {"$set": {"consumed": True}},
    )
    return _serialize(trade_doc)


@router.post("/{trade_id}/close")
async def close_trade(trade_id: str, user=Depends(get_current_user)):
    """Mark a trade as pending-close so the EA closes it on next poll."""
    db = get_db()
    result = await db.trades.update_one(
        {"_id": ObjectId(trade_id), "user_id": user["id"], "status": "open"},
        {"$set": {"status": "pending", "close_requested": True}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Open trade not found")
    return {"ok": True}
