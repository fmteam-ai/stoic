from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from bson import ObjectId
from pydantic import BaseModel

from database import get_db
from models import BridgeHeartbeat, BridgeTradeReport
from ws_manager import manager as ws_manager

router = APIRouter(prefix="/bridge", tags=["bridge"])


async def _account_by_token(token: str) -> dict:
    db = get_db()
    acc = await db.accounts.find_one({"bridge_token": token})
    if not acc:
        raise HTTPException(status_code=401, detail="Invalid bridge token")
    return acc


@router.post("/heartbeat")
async def heartbeat(payload: BridgeHeartbeat):
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.accounts.update_one(
        {"_id": acc["_id"]},
        {"$set": {
            "balance": payload.balance,
            "equity": payload.equity,
            "open_positions": payload.open_positions,
            "status": "connected",
            "last_heartbeat": now_iso,
        }},
    )
    await ws_manager.broadcast(acc["user_id"], "account_heartbeat", {
        "account_id": str(acc["_id"]),
        "balance": payload.balance,
        "equity": payload.equity,
        "open_positions": payload.open_positions,
        "last_heartbeat": now_iso,
    })
    return {"ok": True, "server_time": now_iso}


class PollRequest(BaseModel):
    bridge_token: str


@router.post("/poll-trades")
async def poll_trades(payload: PollRequest):
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)

    # 1. Pending NEW trades (status='pending')
    new_cursor = db.trades.find({"account_id": str(acc["_id"]), "status": "pending"})
    pending = await new_cursor.to_list(length=20)
    out = []
    for t in pending:
        out.append({
            "trade_id": str(t["_id"]),
            "symbol": t["symbol"],
            "action": t["action"],
            "lot_size": t["lot_size"],
            "entry_price": t["entry_price"],
            "stop_loss": t["stop_loss"],
            "take_profit": t["take_profit"],
            "close_requested": t.get("close_requested", False),
            "mt5_ticket": t.get("mt5_ticket"),
        })

    # 2. Open trades with pending modifications (break-even / partial-close / trailing)
    mod_cursor = db.trades.find({
        "account_id": str(acc["_id"]),
        "status": "open",
        "mt5_ticket": {"$ne": None},
        "pending_modification": {"$exists": True, "$ne": None},
    })
    mods = await mod_cursor.to_list(length=20)
    modifications = []
    for t in mods:
        m = t.get("pending_modification") or {}
        modifications.append({
            "trade_id": str(t["_id"]),
            "mt5_ticket": t.get("mt5_ticket"),
            "symbol": t["symbol"],
            "type": m.get("type"),
            "new_sl": m.get("new_sl"),
            "new_tp": m.get("new_tp"),
            "new_volume": m.get("new_volume"),
        })

    return {"trades": out, "modifications": modifications}


class BridgeModificationAck(BaseModel):
    bridge_token: str
    trade_id: str
    type: str   # MODIFY_SL | PARTIAL_CLOSE
    success: bool = True
    new_sl: float | None = None
    new_volume: float | None = None
    error: str | None = None


@router.post("/modification-ack")
async def modification_ack(payload: BridgeModificationAck):
    """EA acknowledges it applied a pending_modification on its end."""
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)
    trade = await db.trades.find_one({"_id": ObjectId(payload.trade_id)})
    if not trade or trade["account_id"] != str(acc["_id"]):
        raise HTTPException(status_code=404, detail="Trade not found")

    update = {"pending_modification": None}
    if payload.success:
        if payload.type == "MODIFY_SL" and payload.new_sl is not None:
            update["stop_loss"] = float(payload.new_sl)
            if not trade.get("breakeven_set"):
                # Mark BE only when SL moved to/past entry
                entry = float(trade.get("entry_price") or 0)
                action = trade.get("action")
                hit_be = (action == "BUY" and payload.new_sl >= entry) or \
                         (action == "SELL" and payload.new_sl <= entry)
                if hit_be:
                    update["breakeven_set"] = True
            else:
                update["trail_active"] = True
        elif payload.type == "PARTIAL_CLOSE" and payload.new_volume is not None:
            update["lot_size"] = float(payload.new_volume)
            update["partial_closed"] = True
    else:
        update["last_modification_error"] = payload.error or "unknown EA error"

    await db.trades.update_one({"_id": ObjectId(payload.trade_id)}, {"$set": update})
    await ws_manager.broadcast(acc["user_id"], "trade_updated", {
        "trade_id": payload.trade_id,
        **update,
    })
    return {"ok": True}


@router.post("/report")
async def report_trade(payload: BridgeTradeReport):
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)
    trade = await db.trades.find_one({"_id": ObjectId(payload.trade_id)})
    if not trade or trade["account_id"] != str(acc["_id"]):
        raise HTTPException(status_code=404, detail="Trade not found")

    update = {"status": payload.status}
    if payload.mt5_ticket is not None:
        update["mt5_ticket"] = payload.mt5_ticket
    if payload.entry_price is not None:
        update["entry_price"] = payload.entry_price
    if payload.exit_price is not None:
        update["exit_price"] = payload.exit_price
    if payload.pnl is not None:
        update["pnl"] = payload.pnl
    if payload.error:
        update["error"] = payload.error
    if payload.status == "closed":
        update["closed_at"] = datetime.now(timezone.utc).isoformat()

    await db.trades.update_one({"_id": ObjectId(payload.trade_id)}, {"$set": update})
    await ws_manager.broadcast(acc["user_id"], "trade_updated", {
        "trade_id": payload.trade_id,
        **update,
    })
    return {"ok": True}
