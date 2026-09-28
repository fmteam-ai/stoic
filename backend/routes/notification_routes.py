"""Notifications routes — per-user Telegram alert settings."""
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
import hashlib
import hmac
import re
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
import httpx

from auth import get_current_user
from database import get_db
from secrets_vault import encrypt as vault_encrypt, decrypt as vault_decrypt

# Telegram MarkdownV2 bodies — raw strings so "\." is a literal escape (audit r16 P2-06)
TELEGRAM_VERIFY_TEXT = ("🔐 STOIC chat verification code: *{code}*\n\n"
                        r"Enter it on the Notifications page\. Expires in 10 minutes\.")
TELEGRAM_TEST_TEXT = (
    r"🧪 *\[TEST\] STOIC AI Trader · Test Alert*" "\n\n"
    r"Your Telegram alerts are working\." "\n"
    "You will now receive push notifications for trades, "
    r"break\-even shifts, partial closes, and circuit breakers\." "\n\n"
    "— STOIC AI"
)

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
        "telegram_verified": bool(doc.get("telegram_verified")),
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


async def _stamp_test(db, user_id: str, channel: str, ok: bool, error: str | None = None,
                      receipt: str | None = None):
    await db.notifications.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id,
                  f"last_test.{channel}": {
                      "ok": ok, "error": error, "provider_receipt": receipt,
                      "at": datetime.now(timezone.utc).isoformat()}}},
        upsert=True)


async def _test_safeguards(db, user: dict, request: Request, channel: str) -> None:
    """Audit r14 P2-05 — delivery tests are user-scoped, step-up gated for
    MFA-enrolled users, need a verified destination, and are audit-chained."""
    full = await db.users.find_one({"_id": ObjectId(user["id"])},
                                   {"two_factor_enabled": 1, "email_verified": 1, "role": 1})
    # affirmative destination verification (r15 P2-02): missing/legacy state is NOT verified
    if channel == "email" and (full or {}).get("email_verified") is not True:
        raise HTTPException(status_code=403, detail={
            "code": "destination_unverified",
            "message": "Verify your email address before sending a test alert to it."})
    if channel == "telegram":
        cfg = await db.notifications.find_one({"user_id": user["id"]}, {"telegram_verified": 1})
        if not (cfg or {}).get("telegram_verified"):
            raise HTTPException(status_code=403, detail={
                "code": "destination_unverified",
                "message": "Verify the Telegram chat first (send the one-time code from this page)."})
    # role-based step-up policy: privileged roles always, others when MFA is enrolled
    if str((full or {}).get("role") or user.get("role") or "").lower() in ALERT_TEST_STEP_UP_ROLES \
            or (full or {}).get("two_factor_enabled"):
        from step_up import require_step_up
        await require_step_up(db, user, request, "alert_test")


async def _audit_test(db, user: dict, channel: str, ok: bool, receipt: str | None, error: str | None):
    from audit_chain import append_chained
    await append_chained(db, {
        "actor_email": user.get("email"), "action": "alert_test_sent",
        "target_kind": "notification_channel", "target_id": channel,
        "meta": {"ok": ok, "provider_receipt": receipt, "error": error, "template": "TEST"},
        "at": datetime.now(timezone.utc).isoformat(),
    }, collection="notification_test_audit")


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
    if payload.telegram_bot_token is not None or payload.telegram_chat_id is not None:
        update["telegram_verified"] = False        # r15 P2-02 — a new binding needs a fresh handshake
        update["telegram_verify"] = None

    if payload.alerts is not None:
        merged = dict(existing.get("alerts") or {})
        merged.update(payload.alerts)
        update["alerts"] = merged

    await db.notifications.update_one(
        {"user_id": user["id"]}, {"$set": update}, upsert=True
    )
    doc = await db.notifications.find_one({"user_id": user["id"]})
    return _serialize(doc)


ALERT_TEST_STEP_UP_ROLES = {"admin", "owner", "ops"}
TEST_ALERT_MAX = 5
TEST_ALERT_WINDOW_SEC = 900


async def _telegram_send(db, user_id: str, text: str) -> tuple[bool, str | None, str | None]:
    """Send MarkdownV2 text to the user's bound chat → (ok, message_id, error)."""
    doc = await db.notifications.find_one({"user_id": user_id})
    if not doc or not doc.get("telegram_bot_token") or not doc.get("telegram_chat_id"):
        return False, None, "telegram_not_configured"
    try:
        token = vault_decrypt(doc["telegram_bot_token"])
    except Exception:
        return False, None, "token_undecryptable"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                "chat_id": doc["telegram_chat_id"], "text": text, "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True})
        except httpx.HTTPError:
            return False, None, "telegram_unreachable"
    if r.status_code != 200:
        try:
            return False, None, (r.json().get("description") or "telegram_rejected")[:120]
        except Exception:
            return False, None, "telegram_rejected"
    try:
        return True, str(((r.json() or {}).get("result") or {}).get("message_id") or ""), None
    except Exception:
        return True, None, None


@router.post("/telegram/verify/start")
async def telegram_verify_start(request: Request, user=Depends(get_current_user)):
    """One-time challenge to the bound chat (r15 P2-02): proves the operator
    controls the destination before any alert — test or real — is trusted."""
    import secrets
    from security import rate_limit
    db = get_db()
    await rate_limit(db, "tg_verify_start", user["id"], 3, 900, request=request,
                     message="Too many verification codes — try again later.")
    code = f"{secrets.randbelow(1000000):06d}"
    ok, msg_id, err = await _telegram_send(db, user["id"], TELEGRAM_VERIFY_TEXT.format(code=code))
    if not ok:
        raise HTTPException(status_code=400 if err == "telegram_not_configured" else 503,
                            detail={"code": err or "telegram_failed", "message": "Could not deliver the verification code."})
    await db.notifications.update_one({"user_id": user["id"]}, {"$set": {
        "telegram_verified": False,
        "telegram_verify": {"hash": hashlib.sha256(f"{user['id']}:{code}".encode()).hexdigest(),
                            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                            "attempts": 0}}})
    return {"ok": True, "sent": True, "provider_receipt": msg_id, "expires_in_sec": 600}


@router.post("/telegram/verify/confirm")
async def telegram_verify_confirm(payload: dict, request: Request, user=Depends(get_current_user)):
    db = get_db()
    code = re.sub(r"\D", "", str(payload.get("code") or ""))
    doc = await db.notifications.find_one({"user_id": user["id"]}) or {}
    ch = doc.get("telegram_verify") or {}
    if not ch or not code:
        raise HTTPException(status_code=400, detail={"code": "no_pending_challenge", "message": "Request a code first."})
    if datetime.fromisoformat(ch["expires_at"]) < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail={"code": "challenge_expired", "message": "Code expired — request a new one."})
    if int(ch.get("attempts") or 0) >= 5:
        raise HTTPException(status_code=429, detail={"code": "too_many_attempts", "message": "Too many attempts — request a new code."})
    if not hmac.compare_digest(ch["hash"], hashlib.sha256(f"{user['id']}:{code}".encode()).hexdigest()):
        await db.notifications.update_one({"user_id": user["id"]}, {"$inc": {"telegram_verify.attempts": 1}})
        raise HTTPException(status_code=400, detail={"code": "wrong_code", "message": "That code is not correct."})
    await db.notifications.update_one({"user_id": user["id"]}, {"$set": {
        "telegram_verified": True, "telegram_verified_at": datetime.now(timezone.utc).isoformat(),
        "telegram_verified_chat_id": doc.get("telegram_chat_id"), "telegram_verify": None}})
    await _audit_test(db, user, "telegram_verify", True, None, None)
    return {"ok": True, "telegram_verified": True}


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
    await _test_safeguards(db, user, request, "email")
    if not email_sender.is_configured():
        await _stamp_test(db, user["id"], "email", False, "email_not_configured")
        raise HTTPException(status_code=503, detail={
            "code": "email_not_configured",
            "message": "Email delivery is not configured on this server."})
    sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    from html import escape as _esc
    res = await email_sender.send_email(
        user["email"],
        "[TEST] STOIC · Test alert — no action required",
        _test_email_html(_esc(str(user.get("name") or "trader"))[:80], sent_at),
        text=f"STOIC test alert sent {sent_at} UTC. Email alerts reach this inbox.")
    if not res.get("ok"):
        await _stamp_test(db, user["id"], "email", False, "email_send_failed")
        await _audit_test(db, user, "email", False, None, str(res.get("error") or "")[:120])
        # 503 (not 502): Cloudflare replaces origin 502s with its own HTML page.
        raise HTTPException(status_code=503, detail={
            "code": "email_send_failed",
            "message": "The email provider rejected the message.",
            "reason": str(res.get("error") or "")[:240]})
    await _stamp_test(db, user["id"], "email", True, receipt=res.get("id"))
    await _audit_test(db, user, "email", True, res.get("id"), None)
    return {"ok": True, "channel": "email", "delivered_to": _mask_email(user["email"]),
            "provider_receipt": res.get("id"), "message": "Test email sent"}


@router.post("/telegram/test")
async def test_telegram(request: Request, user=Depends(get_current_user)):
    """Send a test message to the configured Telegram chat to validate setup."""
    from security import rate_limit
    db = get_db()
    await rate_limit(db, "alert_test_telegram", user["id"], TEST_ALERT_MAX,
                     TEST_ALERT_WINDOW_SEC, request=request,
                     message="Too many test messages — try again in a few minutes.")
    await _test_safeguards(db, user, request, "telegram")
    doc = await db.notifications.find_one({"user_id": user["id"]})
    if not doc or not doc.get("telegram_bot_token") or not doc.get("telegram_chat_id"):
        await _stamp_test(db, user["id"], "telegram", False, "telegram_not_configured")
        raise HTTPException(status_code=400, detail="Telegram not configured")
    try:
        token = vault_decrypt(doc["telegram_bot_token"])
    except Exception:
        raise HTTPException(status_code=500, detail="Could not decrypt bot token")

    text = TELEGRAM_TEST_TEXT
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
        await _audit_test(db, user, "telegram", False, None, detail[:120])
        raise HTTPException(status_code=400, detail=detail)
    try:
        msg_id = str(((r.json() or {}).get("result") or {}).get("message_id") or "")
    except Exception:
        msg_id = None
    await _stamp_test(db, user["id"], "telegram", True, receipt=msg_id)
    await _audit_test(db, user, "telegram", True, msg_id, None)
    return {"ok": True, "channel": "telegram", "provider_receipt": msg_id,
            "message": "Test message sent successfully"}
