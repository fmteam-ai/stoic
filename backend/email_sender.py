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


def _api_key() -> str:   # read lazily — Admin → Integrations can update the sealed vault at runtime
    key = os.environ.get("RESEND_API_KEY", "")
    if key:
        resend.api_key = key
    return key


def _sender() -> str:
    email = os.environ.get("SENDER_EMAIL") or "onboarding@resend.dev"
    name = (os.environ.get("SENDER_NAME") or "").strip()
    return f"{name} <{email}>" if name else email


def is_configured() -> bool:
    return bool(_api_key())


async def send_email(
    recipient: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    sender: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Send a transactional email via Resend.

    Returns: {ok: bool, id?: str, error?: str}
    """
    if not _api_key():
        return {"ok": False, "error": "RESEND_API_KEY not configured"}
    if not recipient:
        return {"ok": False, "error": "missing recipient"}

    params: dict = {
        "from": sender or _sender(),
        "to": [recipient],
        "subject": subject,
        "html": html,
    }
    if text:
        params["text"] = text

    try:
        # r17 P2-02 — provider-bound idempotency: Resend dedupes the exact key
        # (sent as the Idempotency-Key header), so a crash after acceptance but
        # before our outbox acknowledgement cannot produce a second delivery.
        if idempotency_key:
            email = await asyncio.to_thread(resend.Emails.send, params, {"idempotency_key": idempotency_key})
        else:
            email = await asyncio.to_thread(resend.Emails.send, params)
        return {"ok": True, "id": (email or {}).get("id")}
    except Exception as e:
        logger.error("resend send failed: %s", e)
        return {"ok": False, "error": str(e)}
