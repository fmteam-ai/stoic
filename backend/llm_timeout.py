"""Fix plan A2 — every LLM call made from the trading loop runs under a hard timeout
(AI_CALL_TIMEOUT_SEC, default 10 s). A slow model raises asyncio.TimeoutError into the
caller's existing fallback path instead of stalling the cycle."""
import asyncio
import logging
import os

logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT_SEC = 10.0


def ai_timeout_sec() -> float:
    try:
        return max(2.0, float(os.environ.get("AI_CALL_TIMEOUT_SEC") or DEFAULT_TIMEOUT_SEC))
    except ValueError:
        return DEFAULT_TIMEOUT_SEC


async def send_with_timeout(chat, message, *, label: str = "llm", seconds: float | None = None):
    secs = seconds or ai_timeout_sec()
    try:
        return await asyncio.wait_for(chat.send_message(message), timeout=secs)
    except asyncio.TimeoutError:
        logger.warning("AI call %s timed out after %.0fs — using fallback", label, secs)
        raise
