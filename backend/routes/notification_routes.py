"""Notifications routes — per-user Telegram alert settings."""
from datetime import datetime, timezone
from typing import Optional, Dict
from fastapi import APIRouter, Depends, HTTPException, Request
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
    import email_sender
    alerts = dict(DEFAULT_ALERTS)
    alerts.update(doc.get("alerts") or {})
    return {
        "telegram_enabled": doc.get("telegram_enabled", False),
        "telegram_chat_id": doc.get("telegram_chat_id"),
        "has_token": bool(doc.get("telegram_bot_token")),
        "email_configured": email_sender.is_configured(),
        "last_test": doc.get("last_test") or {},
        "alerts": alerts,
        "updated_at": doc.get("updated_at"),
    }


def _mask_email(addr: str) -> str:
    local, _, domain = (addr or "").partition("@")
    if not domain:
        return "***"
    return f"{local[:2]}***@{domain}"


async def _stamp_test(db, user_id: str, channel: str, ok: bool, error: str | None = None):
    await db.notifications.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id,
                  f"last_test.{channel}": {
                      "ok": ok, "error": error,
                      "at": datetime.now(timezone.utc).isoformat()}}},
        upsert=True)


def _test_email_html(name: str, sent_at: str) -> str:
    return f"""
<div style="background:#0A0A0A;color:#FAFAFA;font-family:'Courier New',monospace;padding:32px;max-width:560px;margin:auto;border:1px solid #1F1F1F">
  <div style="color:#00FF41;font-size:20px;font-weight:bold;letter-spacing:4px;margin-bottom:24px">STOIC</div>
  <div style="font-size:16px;margin-bottom:16px">Hi {name},</div>
  <div style="font-size:14px;color:#A1A1AA;line-height:1.6;margin-bottom:16px">
    This is a <span style="color:#FFD700;font-weight:bold">test alert</span> you requested from the Notifications page.
    If you are reading it, email alerts from STOIC reach this inbox.
  </div>
  <div style="font-size:14px;color:#A1A1AA;line-height:1.6;margin-bottom:24px">
    Outage notices, renewal reminders and support replies will arrive the same way.
  </div>
  <div style="font-size:11px;color:#52525B">Sent {sent_at} UTC · STOIC is software, not a broker.</div>
</div>"""


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


TEST_ALERT_MAX = 5
TEST_ALERT_WINDOW_SEC = 900


@router.post("/email/test")
async def test_email(request: Request, user=Depends(get_current_user)):
    """Send a test alert email to the signed-in user's address so they can
    confirm delivery BEFORE a real outage notice needs to reach them."""
    import email_sender
    from security import rate_limit
    db = get_db()
    await rate_limit(db, "alert_test_email", user["id"], TEST_ALERT_MAX,
                     TEST_ALERT_WINDOW_SEC, request=request,
                     message="Too many test emails — try again in a few minutes.")
    if not email_sender.is_configured():
        await _stamp_test(db, user["id"], "email", False, "email_not_configured")
        raise HTTPException(status_code=503, detail={
            "code": "email_not_configured",
            "message": "Email delivery is not configured on this server."})
    sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    res = await email_sender.send_email(
        user["email"],
        "STOIC · Test alert",
        _test_email_html(user.get("name") or "trader", sent_at),
        text=f"STOIC test alert sent {sent_at} UTC. Email alerts reach this inbox.")
    if not res.get("ok"):
        await _stamp_test(db, user["id"], "email", False, "send_failed")
        # 503 (not 502): Cloudflare replaces origin 502s with its own HTML page.
        raise HTTPException(status_code=503, detail={
            "code": "email_send_failed",
            "message": "The email provider rejected the message.",
            "reason": str(res.get("error") or "")[:240]})
    await _stamp_test(db, user["id"], "email", True)
    return {"ok": True, "channel": "email", "delivered_to": _mask_email(user["email"]),
            "message": "Test email sent"}


@router.post("/telegram/test")
async def test_telegram(request: Request, user=Depends(get_current_user)):
    """Send a test message to the configured Telegram chat to validate setup."""
    from security import rate_limit
    db = get_db()
    await rate_limit(db, "alert_test_telegram", user["id"], TEST_ALERT_MAX,
                     TEST_ALERT_WINDOW_SEC, request=request,
                     message="Too many test messages — try again in a few minutes.")
    doc = await db.notifications.find_one({"user_id": user["id"]})
    if not doc or not doc.get("telegram_bot_token") or not doc.get("telegram_chat_id"):
        await _stamp_test(db, user["id"], "telegram", False, "telegram_not_configured")
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
            await _stamp_test(db, user["id"], "telegram", False, "telegram_unreachable")
            from errors import api_error
            raise api_error(503, "telegram_unreachable", "Telegram could not be reached — check the bot token and network.", exc=e)
    if r.status_code != 200:
        detail = "Bad response from Telegram API"
        try:
            j = r.json()
            detail = j.get("description") or detail
        except Exception:
            pass
        await _stamp_test(db, user["id"], "telegram", False, "telegram_rejected")
        raise HTTPException(status_code=400, detail=detail)
    await _stamp_test(db, user["id"], "telegram", True)
    return {"ok": True, "channel": "telegram", "message": "Test message sent successfully"}
