"""Live Trade Journal — AI-written, shareable post-mortem card for any
closed trade.

  POST   /api/journal/{trade_id}/card    — generate (or return cached) card
  GET    /api/journal/{trade_id}/card    — fetch existing card
  DELETE /api/journal/{trade_id}/card    — revoke the public share link
  GET    /api/public/journal/{share_id}  — unauthenticated masked card
"""
import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db
from route_utils import parse_object_id

logger = logging.getLogger("journal")

router = APIRouter(prefix="/journal", tags=["journal"])
public_router = APIRouter(prefix="/public", tags=["public-journal"])

_SYSTEM = """You are STOIC's trade-journal writer. Given one closed trade,
write an honest, punchy post-mortem card a trader would proudly share
publicly. Never invent facts not present in the data. Respond ONLY with
minified JSON, no code fences, exactly these keys:
{"title": "<max 8 words, no ticker spam>",
 "verdict": "WIN"|"LOSS"|"SCRATCH",
 "summary": "<2-3 sentences, plain language>",
 "what_went_right": "<1-2 sentences>",
 "what_went_wrong": "<1-2 sentences, empty string if nothing>",
 "lesson": "<one memorable takeaway sentence>",
 "grade": "A"|"B"|"C"|"D"|"F",
 "hashtags": ["<3-5 tags without #>"]}"""


async def _generate_card(trade: dict) -> dict:
    payload = json.dumps({
        "symbol": trade.get("symbol"), "action": trade.get("action"),
        "entry_price": trade.get("entry_price"),
        "exit_price": trade.get("exit_price"),
        "stop_loss": trade.get("stop_loss"),
        "take_profit": trade.get("take_profit"),
        "lot_size": trade.get("lot_size"), "pnl": trade.get("pnl"),
        "close_reason": trade.get("close_reason"),
        "opened_at": trade.get("opened_at") or trade.get("created_at"),
        "closed_at": trade.get("closed_at"),
        "ai_reasoning_at_entry": (trade.get("reasoning") or "")[:400],
    }, default=str)
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        chat = LlmChat(
            api_key=os.environ["EMERGENT_LLM_KEY"],
            session_id=f"journal-{trade.get('symbol', '?')}-{uuid.uuid4().hex[:8]}",
            system_message=_SYSTEM,
        ).with_model("anthropic", "claude-sonnet-4-5-20250929")
        raw = str(await chat.send_message(UserMessage(text=payload))).strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        card = json.loads(raw)
        card["_llm_failed"] = False
        return card
    except Exception as e:  # noqa: BLE001
        logger.warning("journal LLM failed: %s", e)
        pnl = float(trade.get("pnl") or 0)
        verdict = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "SCRATCH"
        return {
            "title": f"{trade.get('symbol')} {str(trade.get('action', '')).upper()} — {verdict.lower()}",
            "verdict": verdict,
            "summary": (f"Closed {trade.get('symbol')} at "
                        f"{trade.get('exit_price')} for ${pnl:.2f} "
                        f"({trade.get('close_reason') or 'manual close'})."),
            "what_went_right": "", "what_went_wrong": "",
            "lesson": "AI narrative unavailable — numbers speak for themselves.",
            "grade": "B" if pnl > 0 else "D",
            "hashtags": ["stoic", "trading", str(trade.get("symbol", "")).lower()],
            "_llm_failed": True,
        }


def _trade_snapshot(trade: dict) -> dict:
    return {k: trade.get(k) for k in
            ("symbol", "action", "entry_price", "exit_price", "pnl",
             "close_reason", "opened_at", "closed_at", "pnl_source")}


def _payload(doc: dict) -> dict:
    return {"trade_id": doc["trade_id"], "share_id": doc.get("share_id"),
            "revoked": bool(doc.get("revoked")),
            "card": doc.get("card") or {}, "trade": doc.get("trade") or {},
            "created_at": doc.get("created_at")}


async def _owned_closed_trade(db, trade_id: str, user_id: str) -> dict:
    trade = await db.trades.find_one({"_id": parse_object_id(trade_id),
                                      "user_id": user_id})
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.get("status") != "closed" or trade.get("exit_price") is None:
        raise HTTPException(status_code=400,
                            detail="Journal cards are for fully closed trades")
    return trade


@router.post("/{trade_id}/card")
async def create_card(trade_id: str, force: bool = False,
                      user=Depends(get_current_user)):
    db = get_db()
    trade = await _owned_closed_trade(db, trade_id, user["id"])
    existing = await db.trade_journal_cards.find_one(
        {"trade_id": trade_id, "user_id": user["id"]})
    if existing and not force:
        return _payload(existing)
    card = await _generate_card(trade)
    doc = {
        "trade_id": trade_id, "user_id": user["id"],
        "share_id": (existing or {}).get("share_id") or secrets.token_urlsafe(12),
        "revoked": False, "card": card, "trade": _trade_snapshot(trade),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.trade_journal_cards.update_one(
        {"trade_id": trade_id, "user_id": user["id"]},
        {"$set": doc}, upsert=True)
    return _payload(doc)


@router.get("/{trade_id}/card")
async def get_card(trade_id: str, user=Depends(get_current_user)):
    db = get_db()
    doc = await db.trade_journal_cards.find_one(
        {"trade_id": trade_id, "user_id": user["id"]})
    if not doc:
        raise HTTPException(status_code=404, detail="No card yet")
    return _payload(doc)


@router.delete("/{trade_id}/card")
async def revoke_card(trade_id: str, user=Depends(get_current_user)):
    db = get_db()
    res = await db.trade_journal_cards.update_one(
        {"trade_id": trade_id, "user_id": user["id"]},
        {"$set": {"revoked": True}})
    return {"revoked": res.modified_count > 0}


@public_router.get("/journal/{share_id}")
async def public_journal(share_id: str):
    """Unauthenticated read-only journal card (no account identifiers)."""
    db = get_db()
    doc = await db.trade_journal_cards.find_one(
        {"share_id": share_id, "revoked": {"$ne": True}})
    if not doc:
        raise HTTPException(status_code=404,
                            detail="Share link not found or revoked")
    return {"share_id": share_id, "card": doc.get("card") or {},
            "trade": doc.get("trade") or {},
            "created_at": doc.get("created_at")}
