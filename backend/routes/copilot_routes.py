"""AI Co-Pilot routes — grounded chat assistant for the user's trading data."""
import logging
import time
from collections import deque
from typing import Deque, Dict
from fastapi import APIRouter, Depends, HTTPException
from auth import get_current_user
from copilot import chat as copilot_chat, list_sessions, get_session

logger = logging.getLogger("copilot-routes")
router = APIRouter(prefix="/copilot", tags=["copilot"])

# Per-user sliding-window throttle: 30 chats per 5 minutes
_COPILOT_LIMIT = 30
_COPILOT_WINDOW_S = 300
_chat_buckets: Dict[str, Deque[float]] = {}


def _allow_chat(user_id: str) -> bool:
    now = time.time()
    bucket = _chat_buckets.setdefault(user_id, deque())
    cutoff = now - _COPILOT_WINDOW_S
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= _COPILOT_LIMIT:
        return False
    bucket.append(now)
    return True


@router.post("/chat")
async def copilot_chat_endpoint(payload: dict, user=Depends(get_current_user)):
    msg = (payload.get("message") or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="message required")
    if len(msg) > 2000:
        raise HTTPException(status_code=400, detail="message too long (max 2000)")

    if not _allow_chat(user["id"]):
        raise HTTPException(
            status_code=429,
            detail="Slow down — too many Co-Pilot messages. Try again shortly.",
        )

    session_id = payload.get("session_id")
    try:
        result = await copilot_chat(user["id"], msg, session_id)
    except Exception as e:
        logger.exception("copilot.chat failed for user=%s", user["id"])
        raise HTTPException(
            status_code=502,
            detail="Co-Pilot is unavailable right now. Please try again in a moment.",
        ) from e
    return result


@router.get("/sessions")
async def copilot_sessions(user=Depends(get_current_user)):
    return await list_sessions(user["id"])


@router.get("/sessions/{session_id}")
async def copilot_session_detail(session_id: str, user=Depends(get_current_user)):
    s = await get_session(user["id"], session_id)
    if not s:
        raise HTTPException(status_code=404, detail="session not found")
    return s
