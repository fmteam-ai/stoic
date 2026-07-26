"""Resend transactional email helper (iter-69).

Single async helper for sending HTML emails from FastAPI. Mirrors the
playbook pattern: synchronous Resend SDK wrapped in `asyncio.to_thread`
to keep the event loop non-blocking.
"""
from __future__ import annotations
import os
import asyncio
import logging
from typing import Optional

import resend

logger = logging.getLogger("email_sender")

# Configure once on module import.
_API_KEY = os.environ.get("RESEND_API_KEY", "")
_SENDER_EMAIL = os.environ.get("SENDER_EMAIL") or "onboarding@resend.dev"
_SENDER_NAME = (os.environ.get("SENDER_NAME") or "").strip()
_SENDER = f"{_SENDER_NAME} <{_SENDER_EMAIL}>" if _SENDER_NAME else _SENDER_EMAIL
if _API_KEY:
    resend.api_key = _API_KEY


def is_configured() -> bool:
    return bool(_API_KEY)


async def send_email(
    recipient: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    sender: Optional[str] = None,
) -> dict:
    """Send a transactional email via Resend.

    Returns: {ok: bool, id?: str, error?: str}
    """
    if not _API_KEY:
        return {"ok": False, "error": "RESEND_API_KEY not configured"}
    if not recipient:
        return {"ok": False, "error": "missing recipient"}

    params: dict = {
        "from": sender or _SENDER,
        "to": [recipient],
        "subject": subject,
        "html": html,
    }
    if text:
        params["text"] = text

    try:
        email = await asyncio.to_thread(resend.Emails.send, params)
        return {"ok": True, "id": (email or {}).get("id")}
    except Exception as e:
        logger.error("resend send failed: %s", e)
        return {"ok": False, "error": str(e)}
