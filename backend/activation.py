"""Email activation tokens + Terms acceptance for new user signups.

Flow:
  1. User submits /api/auth/register with terms_agreed=true.
  2. We create the user with `email_verified=false` and a 24h activation
     token (`activation_token`, `activation_expires_at`).
  3. We email them a link `${FRONTEND_URL}/verify-email?token=...`.
  4. They click the link → frontend POSTs `/api/auth/verify-email` with the
     token → we flip `email_verified=true`, clear the token, log them in.
  5. Login is refused with HTTP 403 + `account_unverified` until then.
  6. They can re-request the email via POST /api/auth/resend-activation
     (rate-limited 1/min).
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone, timedelta

from email_sender import send_email, is_configured as email_is_configured

TOKEN_SCHEMA = "v2."   # hashed-at-rest token epoch (r25 P2-03); pre-epoch links carry no prefix

logger = logging.getLogger("activation")

ACTIVATION_TTL_HOURS = 24
RESEND_COOLDOWN_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_activation_token() -> tuple[str, str]:
    """Returns (token, expires_at_iso)."""
    token = TOKEN_SCHEMA + secrets.token_urlsafe(32)   # r26 P3-01: schema epoch travels with the link
    exp = (_now() + timedelta(hours=ACTIVATION_TTL_HOURS)).isoformat()
    return token, exp


def _frontend_base() -> str:
    return (
        os.environ.get("FRONTEND_URL")
        or os.environ.get("REACT_APP_BACKEND_URL")
        or "http://localhost:3000"
    ).rstrip("/")


def _activation_link(token: str) -> str:
    return f"{_frontend_base()}/verify-email?token={token}"


def _email_html(*, name: str, link: str) -> str:
    safe_name = (name or "trader").replace("<", "").replace(">", "")
    return f"""\
<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#050505;font-family:-apple-system,Segoe UI,Roboto,sans-serif;">
  <div style="max-width:560px;margin:0 auto;padding:40px 24px;color:#E5E5E5;">
    <div style="font-size:13px;color:#FFD700;letter-spacing:0.3em;font-family:monospace;margin-bottom:24px;">
      STOIC · STEADY WEALTH
    </div>
    <h1 style="color:#FFFFFF;font-size:22px;margin:0 0 16px;">Welcome, {safe_name}.</h1>
    <p style="color:#A1A1AA;line-height:1.6;font-size:15px;margin:0 0 24px;">
      One last step to activate your STOIC membership. Click the button below
      to confirm your email and unlock the dashboard. This link expires in
      <strong style="color:#FFD700;">24 hours</strong>.
    </p>
    <div style="text-align:center;margin:32px 0;">
      <a href="{link}"
         style="display:inline-block;background:#00FF41;color:#000;font-weight:bold;
                padding:14px 32px;text-decoration:none;font-family:monospace;
                letter-spacing:0.2em;font-size:13px;border:none;">
        ACTIVATE MEMBERSHIP →
      </a>
    </div>
    <p style="color:#52525B;font-size:12px;line-height:1.6;margin:24px 0 8px;">
      If the button doesn't work, paste this URL into your browser:
    </p>
    <p style="color:#A1A1AA;font-size:12px;word-break:break-all;font-family:monospace;
              background:#0A0A0A;border:1px solid #1F1F1F;padding:10px;border-radius:0;">
      {link}
    </p>
    <hr style="border:none;border-top:1px solid #1F1F1F;margin:32px 0;">
    <p style="color:#52525B;font-size:11px;line-height:1.6;">
      You're receiving this because someone (hopefully you) signed up at STOIC.
      If this wasn't you, you can safely ignore this email — no account is
      activated until the link above is clicked.
    </p>
    <p style="color:#52525B;font-size:11px;font-family:monospace;letter-spacing:0.2em;margin-top:24px;">
      STOIC · DISCIPLINED AI TRADING
    </p>
  </div>
</body></html>"""


def _email_text(*, link: str) -> str:
    return (
        "Welcome to STOIC.\n\n"
        "Activate your membership by visiting this link within 24 hours:\n\n"
        f"{link}\n\n"
        "If you didn't sign up, ignore this email — no account is activated "
        "until the link above is clicked.\n\n"
        "— STOIC · Disciplined AI Trading"
    )


async def send_existing_account_email(*, recipient: str) -> dict:
    """Out-of-band notice when someone registers with an already-used email."""
    if not email_is_configured():
        logger.warning("RESEND_API_KEY not configured — existing-account notice for %s skipped", recipient)
        return {"ok": False, "error": "email_not_configured"}
    login_url = f"{_frontend_base()}/login"
    text = (
        "Someone tried to create a STOIC account with this email address, "
        "but an account already exists.\n\n"
        f"If that was you, sign in here: {login_url}\n"
        "If you forgot your password, use the reset link on the sign-in page.\n\n"
        "If this wasn't you, no action is needed — nothing has changed.\n\n"
        "— STOIC · Disciplined AI Trading"
    )
    return await send_email(
        recipient=recipient,
        subject="You already have a STOIC account",
        html=f"<pre style='font-family:sans-serif;white-space:pre-wrap'>{text}</pre>",
        text=text,
    )


NOTICE_COOLDOWN_SECONDS = 24 * 3600


async def queue_existing_account_notice(db, email: str) -> None:
    """At most ONE notice per normalized email per cooldown; sent in the
    background so response timing does not depend on delivery."""
    import asyncio
    now = datetime.now(timezone.utc)
    key = hashlib.sha256(email.encode()).hexdigest()
    res = await db.registration_notices.update_one(
        {"_id": key, "$or": [{"sent_at": {"$exists": False}},
                            {"sent_at": {"$lte": (now - timedelta(seconds=NOTICE_COOLDOWN_SECONDS)).isoformat()}}]},
        {"$set": {"sent_at": now.isoformat()}, "$inc": {"attempts": 1}})
    if res.matched_count == 0:
        # either first time (insert) or within cooldown (skip)
        try:
            await db.registration_notices.insert_one({"_id": key, "sent_at": now.isoformat(), "attempts": 1})
        except Exception:  # noqa: BLE001 — exists → within cooldown
            await db.registration_notices.update_one({"_id": key}, {"$inc": {"suppressed": 1}})
            return
    asyncio.get_event_loop().create_task(send_existing_account_email(recipient=email))


async def queue_activation_email(*, recipient: str, name: str, token: str) -> str | None:
    """Background send; returns the dev-only link when email is unconfigured."""
    import asyncio
    if not email_is_configured():
        return (await send_activation_email(recipient=recipient, name=name, token=token)).get("activation_link_dev_only")

    async def _go():
        res = await send_activation_email(recipient=recipient, name=name, token=token)
        if not res.get("ok"):
            logger.warning("activation email not delivered (%s)", res.get("error"))
    asyncio.get_event_loop().create_task(_go())
    return None


async def send_activation_email(*, recipient: str, name: str, token: str) -> dict:
    """Fire the activation email. Returns the Resend send result."""
    link = _activation_link(token)
    if not email_is_configured():
        # In dev, surface the link in logs so the user can still complete
        # the flow without configuring Resend.
        logger.warning(
            "RESEND_API_KEY not configured — activation link for %s: %s",
            recipient, link,
        )
        return {"ok": False, "error": "email_not_configured",
                "activation_link_dev_only": link}
    return await send_email(
        recipient=recipient,
        subject="Activate your STOIC account",
        html=_email_html(name=name, link=link),
        text=_email_text(link=link),
    )
