"""Notifications routes — per-user Telegram alert settings."""
from datetime import datetime, timezone
from typing import Optional, Dict
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import httpx

from auth import get_current_user
from database import get_db
from secrets_vault import encrypt as vault_encrypt, decrypt as vault_decrypt

router = APIRouter(prefix="/notifications", tags=["notifications"])


DEFAULT_ALERTS = {
    "trade_opened": True,
    "trade_closed": True,
    "breakeven": True,
    "partial_close": True,
    "trail": False,                # trailing fires often — opt-in
    "circuit_breaker": True,
    "high_conf_signal": True,
    "external_trade_opened": True, # manual MT5 trade detected (clearly labelled)
}


class TelegramConfigIn(BaseModel):
    telegram_bot_token: Optional[str] = None     # "" clears
    telegram_chat_id: Optional[str] = None       # "" clears
    telegram_enabled: bool = True
    alerts: Optional[Dict[str, bool]] = None


def _serialize(doc: dict) -> dict:
    alerts = dict(DEFAULT_ALERTS)
    alerts.update(doc.get("alerts") or {})
    return {
        "telegram_enabled": doc.get("telegram_enabled", False),
        "telegram_chat_id": doc.get("telegram_chat_id"),
        "has_token": bool(doc.get("telegram_bot_token")),
        "alerts": alerts,
        "updated_at": doc.get("updated_at"),
    }


@router.get("/telegram")
@router.get("/prefs")     # alias — friendlier name for the UI / docs
@router.get("/settings")  # alias — legacy frontend callers
async def get_telegram(user=Depends(get_current_user)):
    db = get_db()
    doc = await db.notifications.find_one({"user_id": user["id"]}) or {}
    return _serialize(doc)


@router.put("/telegram")
async def update_telegram(payload: TelegramConfigIn, user=Depends(get_current_user)):
    db = get_db()
    existing = await db.notifications.find_one({"user_id": user["id"]}) or {}

    update = {
        "user_id": user["id"],
        "telegram_enabled": payload.telegram_enabled,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    if payload.telegram_bot_token is not None:
        if payload.telegram_bot_token == "":
            update["telegram_bot_token"] = None
        else:
            update["telegram_bot_token"] = vault_encrypt(payload.telegram_bot_token.strip())

    if payload.telegram_chat_id is not None:
        update["telegram_chat_id"] = payload.telegram_chat_id.strip() or None

    if payload.alerts is not None:
        merged = dict(existing.get("alerts") or {})
        merged.update(payload.alerts)
        update["alerts"] = merged

    await db.notifications.update_one(
        {"user_id": user["id"]}, {"$set": update}, upsert=True
    )
    doc = await db.notifications.find_one({"user_id": user["id"]})
    return _serialize(doc)


@router.post("/telegram/test")
async def test_telegram(user=Depends(get_current_user)):
    """Send a test message to the configured Telegram chat to validate setup."""
    db = get_db()
    doc = await db.notifications.find_one({"user_id": user["id"]})
    if not doc or not doc.get("telegram_bot_token") or not doc.get("telegram_chat_id"):
        raise HTTPException(status_code=400, detail="Telegram not configured")
    try:
        token = vault_decrypt(doc["telegram_bot_token"])
    except Exception:
        raise HTTPException(status_code=500, detail="Could not decrypt bot token")

    text = (
        "✅ *STOIC AI Trader · Test Alert*\n\n"
        "Your Telegram alerts are working\\.\n"
        "You will now receive push notifications for trades, "
        "break\\-even shifts, partial closes, and circuit breakers\\.\n\n"
        "— STOIC AI"
    )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.post(url, json={
                "chat_id": doc["telegram_chat_id"],
                "text": text,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            })
        except httpx.HTTPError as e:
            from errors import api_error
            raise api_error(502, "telegram_unreachable", "Telegram could not be reached — check the bot token and network.", exc=e)
    if r.status_code != 200:
        detail = "Bad response from Telegram API"
        try:
            j = r.json()
            detail = j.get("description") or detail
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=detail)
    return {"ok": True, "message": "Test message sent successfully"}
