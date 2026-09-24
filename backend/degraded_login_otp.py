"""Degraded-login one-time code (audit round 9 P1-03).

Used ONLY when Turnstile's provider is unavailable and
TURNSTILE_LOGIN_DEGRADED_POLICY=otp_required. Runs AFTER the password has
been verified. The challenge is short-lived, single-use and bound to the user,
the (salted) client IP + user-agent context and the degraded reason; a code
issued in one context cannot complete a login from another.
"""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException

TTL_MINUTES = 5
MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 30
CODE_SENT = "turnstile_degraded_otp_sent"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _salt() -> str:
    return os.environ.get("LEDGER_ANCHOR_KEY") or os.environ.get("JWT_SECRET") or ""


def context_hash(remote_ip: str | None, user_agent: str | None) -> str:
    return hashlib.sha256(f"{_salt()}|{remote_ip or ''}|{(user_agent or '')[:200]}".encode()).hexdigest()


def _code_hash(uid: str, ctx: str, reason: str, code: str) -> str:
    return hashlib.sha256(f"{_salt()}:{uid}:{ctx}:{reason}:{code}".encode()).hexdigest()


def _valid(doc: dict | None) -> bool:
    if not doc:
        return False
    try:
        exp = datetime.fromisoformat(doc["expires_at"])
    except (KeyError, ValueError):
        return False
    return exp > _now() and doc.get("attempts", 0) < MAX_ATTEMPTS


async def _issue(db, user: dict, ctx: str, reason: str) -> None:
    import email_sender
    uid = str(user["_id"])
    code = f"{secrets.randbelow(1000000):06d}"
    now = _now()
    text = (f"Your STOIC sign-in code is {code}. It expires in {TTL_MINUTES} minutes.\n\n"
            "Our human-verification provider is temporarily unavailable, so this code "
            "stands in for the challenge. If you didn't try to sign in, change your password.")
    result = await email_sender.send_email(
        recipient=user["email"], subject=f"{code} is your STOIC sign-in code",
        html=f"<pre style='font-family:sans-serif;white-space:pre-wrap'>{text}</pre>", text=text)
    if not result.get("ok"):
        raise HTTPException(status_code=503, detail={
            "code": "turnstile_unavailable", "retryable": True, "state": "provider_unavailable",
            "message": "Human verification is unavailable and the fallback code could not be emailed. Try again shortly."})
    await db.degraded_login_otps.update_one(
        {"user_id": uid},
        {"$set": {"user_id": uid, "ctx": ctx, "reason": reason,
                  "code_hash": _code_hash(uid, ctx, reason, code),
                  "expires_at": (now + timedelta(minutes=TTL_MINUTES)).isoformat(),
                  "attempts": 0, "last_sent_at": now.isoformat(), "created_at": now.isoformat()}},
        upsert=True)


async def challenge(db, user: dict, provided: str | None, *, remote_ip: str | None,
                    user_agent: str | None, reason: str) -> None:
    """Raise 401 {code: turnstile_degraded_otp_sent} until a valid bound code is
    supplied; return silently once satisfied (single use)."""
    from security import check_failure_limit, record_failure
    uid = str(user["_id"])
    ctx = context_hash(remote_ip, user_agent)
    now = _now()
    doc = await db.degraded_login_otps.find_one({"user_id": uid})
    provided = (provided or "").strip()
    if provided:
        await check_failure_limit(db, "degraded_otp_verify", uid, 10, 900,
                                  "Too many code attempts. Try again in a few minutes.")
        bound = _valid(doc) and doc.get("ctx") == ctx and doc.get("reason") == reason
        if not bound or not hmac.compare_digest(_code_hash(uid, ctx, reason, provided), doc.get("code_hash") or ""):
            await record_failure(db, "degraded_otp_verify", uid, 900)
            attempts = (doc or {}).get("attempts", 0) + 1
            if not _valid(doc) or attempts >= MAX_ATTEMPTS:
                await db.degraded_login_otps.delete_many({"user_id": uid})
                raise HTTPException(status_code=401, detail={
                    "code": "turnstile_degraded_otp_expired",
                    "message": "That code expired or had too many wrong attempts. Request a new one."})
            await db.degraded_login_otps.update_one({"user_id": uid}, {"$set": {"attempts": attempts}})
            raise HTTPException(status_code=401, detail={
                "code": "turnstile_degraded_otp_invalid", "attempts_left": MAX_ATTEMPTS - attempts,
                "message": "Invalid code. Check the email and try again."})
        await db.degraded_login_otps.delete_many({"user_id": uid})
        return
    if _valid(doc) and doc.get("ctx") == ctx:
        try:
            last = datetime.fromisoformat(doc.get("last_sent_at") or "")
        except ValueError:
            last = now - timedelta(seconds=RESEND_COOLDOWN_SECONDS + 1)
        elapsed = (now - last).total_seconds()
        if elapsed < RESEND_COOLDOWN_SECONDS:
            raise HTTPException(status_code=401, detail={
                "code": CODE_SENT, "degraded": True, "resend_in": int(RESEND_COOLDOWN_SECONDS - elapsed),
                "message": "Human verification is unavailable — enter the 6-digit code we emailed you."})
    await check_failure_limit(db, "degraded_otp_issue", uid, 6, 900,
                              "Too many sign-in codes requested. Try again in a few minutes.")
    await _issue(db, user, ctx, reason)
    await record_failure(db, "degraded_otp_issue", uid, 900)
    raise HTTPException(status_code=401, detail={
        "code": CODE_SENT, "degraded": True, "resend_in": RESEND_COOLDOWN_SECONDS,
        "message": "Human verification is unavailable — we emailed you a 6-digit sign-in code. Enter it below."})
