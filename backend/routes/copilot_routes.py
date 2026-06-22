"""AI Co-Pilot routes — grounded chat assistant for the user's trading data."""
from fastapi import APIRouter, Depends, HTTPException
from auth import get_current_user
from copilot import chat as copilot_chat, list_sessions, get_session

router = APIRouter(prefix="/copilot", tags=["copilot"])


@router.post("/chat")
async def copilot_chat_endpoint(payload: dict, user=Depends(get_current_user)):
    msg = (payload.get("message") or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="message required")
    if len(msg) > 2000:
        raise HTTPException(status_code=400, detail="message too long (max 2000)")
    session_id = payload.get("session_id")
    try:
        result = await copilot_chat(user["id"], msg, session_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Co-Pilot failed: {e}")
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
