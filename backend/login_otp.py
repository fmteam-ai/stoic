"""Email one-time-password login gate (iter-131).

When enabled by the admin (db.platform_state {_id:"login_email_otp"}), every
password login for users WITHOUT authenticator 2FA must be confirmed with a
6-digit code emailed to the account address. TOTP-enrolled users skip it.

Storage: db.login_otps — one doc per user, replaced on each issue:
  {user_id, code_hash, expires_at, attempts, last_sent_at, created_at}
"""
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException

logger = logging.getLogger("login_otp")

OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN_SECONDS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(user_id: str, code: str) -> str:
    return hashlib.sha256(f"{user_id}:{code}".encode()).hexdigest()


async def is_enabled(db) -> bool:
    doc = await db.platform_state.find_one({"_id": "login_email_otp"})
    return bool(doc and doc.get("enabled"))


async def set_enabled(db, enabled: bool, actor_email: str = "") -> None:
    await db.platform_state.update_one(
        {"_id": "login_email_otp"},
        {"$set": {"enabled": bool(enabled),
                  "updated_at": _now().isoformat(),
                  "updated_by": actor_email}},
        upsert=True,
    )


def _otp_email_html(name: str, code: str) -> str:
    return f"""
<div style="background:#0A0A0A;color:#FAFAFA;font-family:'Courier New',monospace;padding:32px;max-width:520px;margin:auto;border:1px solid #1F1F1F">
  <div style="color:#00FF41;font-size:20px;font-weight:bold;letter-spacing:4px;margin-bottom:24px">STOIC</div>
  <div style="font-size:15px;margin-bottom:16px">Hi {name},</div>
  <div style="font-size:13px;color:#A1A1AA;line-height:1.6;margin-bottom:20px">
    Your one-time sign-in code is:
  </div>
  <div style="background:#0F0F0F;border:1px solid #00FF41;color:#00FF41;font-size:32px;font-weight:bold;letter-spacing:12px;text-align:center;padding:16px;margin-bottom:20px">{code}</div>
  <div style="font-size:12px;color:#A1A1AA;line-height:1.6">
    It expires in {OTP_TTL_MINUTES} minutes. If you didn't try to sign in,
    change your password immediately.
  </div>
  <div style="font-size:11px;color:#52525B;margin-top:24px">
    STOIC never asks for this code by phone, chat or email.
  </div>
</div>"""


async def _issue(db, user: dict) -> None:
    """Generate + email a fresh code, replacing any prior one."""
    import email_sender
    uid = str(user["_id"])
    code = f"{secrets.randbelow(1000000):06d}"
    now = _now()
    result = await email_sender.send_email(
        recipient=user["email"],
        subject=f"{code} is your STOIC sign-in code",
        html=_otp_email_html(user.get("name") or "trader", code),
    )
    if not result.get("ok"):
        logger.error("login OTP email failed for %s: %s",
                     user["email"], result.get("error"))
        raise HTTPException(status_code=502, detail={
            "code": "email_otp_send_failed",
            "message": "We couldn't send the sign-in code email. "
                       "Try again shortly or contact support.",
        })
    await db.login_otps.update_one(
        {"user_id": uid},
        {"$set": {
            "user_id": uid,
            "code_hash": _hash(uid, code),
            "expires_at": (now + timedelta(minutes=OTP_TTL_MINUTES)).isoformat(),
            "attempts": 0,
            "last_sent_at": now.isoformat(),
            "created_at": now.isoformat(),
        }},
        upsert=True,
    )


async def otp_gate(db, user: dict, provided: str | None) -> None:
    """Run inside /auth/login AFTER password verification, for users
    without TOTP. Raises 401 with a structured detail until a valid
    code is supplied; returns silently once satisfied (or disabled)."""
    if not await is_enabled(db):
        return
    # Admins are exempt — an undeliverable email would otherwise lock the
    # platform out of the very toggle that disables this gate. Admins are
    # expected to enroll authenticator (TOTP) 2FA instead.
    if user.get("role") == "admin":
        return
    uid = str(user["_id"])
    now = _now()
    doc = await db.login_otps.find_one({"user_id": uid})

    def _valid(d) -> bool:
        if not d:
            return False
        try:
            exp = datetime.fromisoformat(d["expires_at"])
        except (KeyError, ValueError):
            return False
        return exp > now and d.get("attempts", 0) < OTP_MAX_ATTEMPTS

    provided = (provided or "").strip()
    if provided:
        from security import check_failure_limit, record_failure
        await check_failure_limit(
            db, "email_otp_verify", uid, 10, 900,
            "Too many code attempts. Try again in a few minutes.")
        if not _valid(doc):
            await db.login_otps.delete_many({"user_id": uid})
            raise HTTPException(status_code=401, detail={
                "code": "email_otp_expired",
                "message": "That code expired or had too many wrong attempts. "
                           "Request a new one.",
            })
        if not hmac.compare_digest(_hash(uid, provided),
                                   doc.get("code_hash") or ""):
            await record_failure(db, "email_otp_verify", uid, 900)
            attempts = doc.get("attempts", 0) + 1
            if attempts >= OTP_MAX_ATTEMPTS:
                await db.login_otps.delete_many({"user_id": uid})
                raise HTTPException(status_code=401, detail={
                    "code": "email_otp_expired",
                    "message": "Too many wrong attempts. Request a new code.",
                })
            await db.login_otps.update_one(
                {"user_id": uid}, {"$set": {"attempts": attempts}})
            raise HTTPException(status_code=401, detail={
                "code": "invalid_email_otp",
                "message": "Invalid code. Check the email and try again.",
                "attempts_left": OTP_MAX_ATTEMPTS - attempts,
            })
        # success — single use
        await db.login_otps.delete_many({"user_id": uid})
        return

    # No code provided: send (or apply resend cooldown to) a challenge.
    if _valid(doc):
        try:
            last = datetime.fromisoformat(doc.get("last_sent_at") or "")
        except ValueError:
            last = now - timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS + 1)
        elapsed = (now - last).total_seconds()
        if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
            raise HTTPException(status_code=401, detail={
                "code": "email_otp_sent",
                "message": "Enter the 6-digit code we emailed you.",
                "resend_in": int(OTP_RESEND_COOLDOWN_SECONDS - elapsed),
            })
    # Volume cap: max 6 emailed codes per account per 15 min (SEC-001).
    from security import check_failure_limit
    await check_failure_limit(
        db, "email_otp_issue", uid, 6, 900,
        "Too many sign-in codes requested. Try again in a few minutes.")
    await _issue(db, user)
    from security import record_failure
    await record_failure(db, "email_otp_issue", uid, 900)
    raise HTTPException(status_code=401, detail={
        "code": "email_otp_sent",
        "message": "We emailed you a 6-digit sign-in code. Enter it below.",
        "resend_in": OTP_RESEND_COOLDOWN_SECONDS,
    })
