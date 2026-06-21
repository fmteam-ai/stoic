"""MT5 Expert Advisor bridge endpoints.

The downloadable MT5 EA polls these endpoints with its bridge_token to:
  - send heartbeat / account snapshot
  - fetch pending trades to execute
  - report trade execution results / P&L

No JWT auth — auth happens via the per-account bridge_token.
"""
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from bson import ObjectId
from typing import Optional
from pydantic import BaseModel

from database import get_db
from models import BridgeHeartbeat, BridgeTradeReport

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
    return {"ok": True, "server_time": now_iso}


class PollRequest(BaseModel):
    bridge_token: str


@router.post("/poll-trades")
async def poll_trades(payload: PollRequest):
    """EA polls for pending trades on this account."""
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)
    cursor = db.trades.find({
        "account_id": str(acc["_id"]),
        "status": "pending",
    })
    pending = await cursor.to_list(length=20)
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
    return {"trades": out}


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
    return {"ok": True}
