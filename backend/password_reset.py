"""Password reset — email-link flow.

Mirrors `activation.py`:
  1. POST /api/auth/forgot-password {email} → generate a 1h reset token,
     email a link `${FRONTEND_URL}/reset-password?token=...`. Returns a
     generic success regardless of whether the email exists (enumeration
     protection). 60s per-account cooldown.
  2. POST /api/auth/reset-password {token, new_password} → consume token,
     hash + set new password, clear token, log a structured audit event.
     Logs the user out of existing sessions by rotating their JWT? Not
     today — we keep it simple: new password works on next login.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone, timedelta

from email_sender import send_email, is_configured as email_is_configured

logger = logging.getLogger("password_reset")

RESET_TTL_HOURS = 1
RESET_RESEND_COOLDOWN_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_reset_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    exp = (_now() + timedelta(hours=RESET_TTL_HOURS)).isoformat()
    return token, exp


def _frontend_base() -> str:
    return (
        os.environ.get("FRONTEND_URL")
        or os.environ.get("REACT_APP_BACKEND_URL")
        or "http://localhost:3000"
    ).rstrip("/")


def _reset_link(token: str) -> str:
    return f"{_frontend_base()}/reset-password?token={token}"


def _email_html(*, name: str, link: str) -> str:
    safe_name = (name or "trader").replace("<", "").replace(">", "")
    return f"""\
<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#050505;font-family:-apple-system,Segoe UI,Roboto,sans-serif;">
  <div style="max-width:560px;margin:0 auto;padding:40px 24px;color:#E5E5E5;">
    <div style="font-size:13px;color:#FFD700;letter-spacing:0.3em;font-family:monospace;margin-bottom:24px;">
      STOIC · STEADY WEALTH
    </div>
    <h1 style="color:#FFFFFF;font-size:22px;margin:0 0 16px;">Password reset request</h1>
    <p style="color:#A1A1AA;line-height:1.6;font-size:15px;margin:0 0 24px;">
      Hi {safe_name}, we received a request to reset the password on your
      STOIC account. Click the button below to choose a new one. This link
      expires in <strong style="color:#FFD700;">1 hour</strong>.
    </p>
    <div style="text-align:center;margin:32px 0;">
      <a href="{link}"
         style="display:inline-block;background:#FFD700;color:#000;font-weight:bold;
                padding:14px 32px;text-decoration:none;font-family:monospace;
                letter-spacing:0.2em;font-size:13px;">
        RESET PASSWORD →
      </a>
    </div>
    <p style="color:#52525B;font-size:12px;line-height:1.6;margin:24px 0 8px;">
      If the button doesn't work, paste this URL into your browser:
    </p>
    <p style="color:#A1A1AA;font-size:12px;word-break:break-all;font-family:monospace;
              background:#0A0A0A;border:1px solid #1F1F1F;padding:10px;">
      {link}
    </p>
    <hr style="border:none;border-top:1px solid #1F1F1F;margin:32px 0;">
    <p style="color:#52525B;font-size:11px;line-height:1.6;">
      <strong style="color:#FF6B6B;">Didn't request this?</strong> Ignore this email — your password
      stays unchanged. For your security, the link expires in 1 hour and can
      only be used once.
    </p>
    <p style="color:#52525B;font-size:11px;font-family:monospace;letter-spacing:0.2em;margin-top:24px;">
      STOIC · DISCIPLINED AI TRADING
    </p>
  </div>
</body></html>"""


def _email_text(*, link: str) -> str:
    return (
        "Password reset for your STOIC account.\n\n"
        "Visit this link within 1 hour to choose a new password:\n\n"
        f"{link}\n\n"
        "If you didn't request this, ignore this email — your password "
        "stays unchanged.\n\n"
        "— STOIC · Disciplined AI Trading"
    )


async def send_reset_email(*, recipient: str, name: str, token: str) -> dict:
    link = _reset_link(token)
    if not email_is_configured():
        logger.warning(
            "RESEND_API_KEY not configured — reset link for %s: %s",
            recipient, link,
        )
        return {"ok": False, "error": "email_not_configured",
                "reset_link_dev_only": link}
    return await send_email(
        recipient=recipient,
        subject="Reset your STOIC password",
        html=_email_html(name=name, link=link),
        text=_email_text(link=link),
    )
