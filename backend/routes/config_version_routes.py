"""iter-104 · Immutable config versions + atomic rollback pointer."""
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/config", tags=["config-versions"])


@router.get("/versions")
async def list_versions(account_id: str | None = None,
                        user=Depends(get_current_user)):
    db = get_db()
    from config_promotion import _pointer_id
    ptr = await db.config_pointers.find_one(
        {"_id": _pointer_id(user["id"], account_id)}) or {}
    q = {"user_id": user["id"]}
    if account_id:
        q["account_id"] = account_id
    versions = []
    async for v in db.config_versions.find(
            q, {"config": 0}).sort("created_at", -1).limit(20):
        vid = str(v["_id"])
        versions.append({
            "id": vid, "label": v.get("label"), "source": v.get("source"),
            "config_hash": v.get("config_hash"),
            "created_at": (v.get("created_at").isoformat()
                           if hasattr(v.get("created_at"), "isoformat")
                           else v.get("created_at")),
            "is_active": vid == ptr.get("active_version_id"),
            "is_rollback_target": vid == ptr.get("previous_version_id")})
    return {"versions": versions,
            "pointer": {"active_version_id": ptr.get("active_version_id"),
                        "previous_version_id":
                            ptr.get("previous_version_id")}}


@router.post("/rollback")
async def rollback_config(request: Request, account_id: str | None = None,
                          user=Depends(get_current_user)):
    """Step-up-MFA'd: re-apply the previous immutable config version."""
    db = get_db()
    from step_up import require_step_up
    await require_step_up(db, user, request, "live_activation")
    from config_promotion import rollback
    try:
        return await rollback(db, user["id"], account_id, actor=user["id"])
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))  # ValueError: crafted validation text
