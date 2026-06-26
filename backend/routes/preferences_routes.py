"""User preferences — small toggles persisted on the user doc (Simple Mode,
sidebar collapse map, theme, etc). Kept separate from /auth so the auth
schema stays minimal."""
from fastapi import APIRouter, Depends, HTTPException
from datetime import datetime, timezone
from bson import ObjectId

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/settings", tags=["settings"])


async def _user_doc(db, user_id: str):
    try:
        d = await db.users.find_one({"_id": ObjectId(user_id)})
        if d:
            return d
    except Exception:
        pass
    return (await db.users.find_one({"_id": user_id})
            or await db.users.find_one({"id": user_id}))


@router.get("/preferences")
async def get_preferences(user=Depends(get_current_user)):
    db = get_db()
    u = await _user_doc(db, user["id"]) or {}
    p = u.get("preferences") or {}
    return {
        "simple_mode": bool(p.get("simple_mode", False)),
        "updated_at": p.get("updated_at"),
    }


@router.post("/preferences")
async def set_preferences(payload: dict, user=Depends(get_current_user)):
    db = get_db()
    u = await _user_doc(db, user["id"])
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    # Whitelist the fields the UI can set. Anything else is ignored.
    update = {}
    if "simple_mode" in payload:
        update["preferences.simple_mode"] = bool(payload["simple_mode"])
    update["preferences.updated_at"] = datetime.now(timezone.utc).isoformat()
    await db.users.update_one({"_id": u["_id"]}, {"$set": update})
    return await get_preferences(user)
