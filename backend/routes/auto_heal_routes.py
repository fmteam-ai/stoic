"""Auto-heal API + scheduler hooks."""
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db
from auto_heal import sweep_user, _user_doc
from datetime import datetime, timezone
from entitlements import require_feature

# iter-60: Auto-Heal is a Pro+ feature.
router = APIRouter(prefix="/auto-heal", tags=["auto-heal"],
                   dependencies=[Depends(require_feature("auto_heal"))])


@router.get("/settings")
async def get_settings(user=Depends(get_current_user)):
    db = get_db()
    u = await _user_doc(db, user["id"]) or {}
    s = u.get("auto_heal_settings") or {}
    return {
        "enabled": bool(s.get("enabled", False)),
        "last_run_at": s.get("last_run_at"),
    }


@router.post("/settings")
async def set_settings(payload: dict, user=Depends(get_current_user)):
    db = get_db()
    u = await _user_doc(db, user["id"])
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    enabled = bool(payload.get("enabled"))
    await db.users.update_one(
        {"_id": u["_id"]},
        {"$set": {"auto_heal_settings": {
            "enabled": enabled,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }}},
    )
    return {"enabled": enabled}


@router.post("/run-now")
async def run_now(user=Depends(get_current_user)):
    """Manual trigger — bypasses the opt-in flag (the user explicitly clicked
    a button). Returns the actions taken in this sweep.
    """
    db = get_db()
    u = await _user_doc(db, user["id"])
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    # Temporarily flip enabled so sweep_user runs even for opt-out users
    s = u.get("auto_heal_settings") or {}
    was_enabled = s.get("enabled", False)
    if not was_enabled:
        await db.users.update_one(
            {"_id": u["_id"]},
            {"$set": {"auto_heal_settings.enabled": True}},
        )
    try:
        result = await sweep_user(user["id"])
    finally:
        if not was_enabled:
            await db.users.update_one(
                {"_id": u["_id"]},
                {"$set": {"auto_heal_settings.enabled": False}},
            )
    return result


@router.get("/log")
async def get_log(limit: int = 50, user=Depends(get_current_user)):
    """Recent auto-heal actions taken on this user's account."""
    db = get_db()
    cur = db.auto_heal_actions.find({"user_id": user["id"]}).sort("created_at", -1).limit(min(limit, 200))
    docs = await cur.to_list(length=200)
    return {"items": [
        {**{k: v for k, v in d.items() if k != "_id"}, "id": str(d["_id"])}
        for d in docs
    ]}
