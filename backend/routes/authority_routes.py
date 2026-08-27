"""Global Trading Authority API — /api/authority"""
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from database import get_db
from trading_authority import (LEVELS, compute_authority, level_severity,
                               set_platform_level)

router = APIRouter(prefix="/authority", tags=["authority"])


@router.get("")
async def authority_ep(user=Depends(get_current_user)):
    db = get_db()
    return await compute_authority(db)


@router.post("/platform")
async def set_platform_ep(payload: dict, request: Request,
                          user=Depends(get_current_user)):
    """Set the platform-wide authority override. Restricting (safer) is a
    single admin action; RELAXING (riskier) additionally requires
    step-up MFA."""
    db = get_db()
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    level = str(payload.get("level") or "").upper()
    if level not in LEVELS:
        raise HTTPException(status_code=400,
                            detail=f"level must be one of {LEVELS}")
    prev = await db.platform_state.find_one({"_id": "trading_authority"})
    prev_level = (prev or {}).get("level") or "FULL"
    if level_severity(level) < level_severity(prev_level):
        from step_up import require_step_up
        await require_step_up(db, user, request, "authority_relax")
    from step_up import audit_event
    await audit_event(db, user["id"], "trading_authority_platform",
                      {"from": prev_level, "to": level,
                       "reason": payload.get("reason")}, request)
    return await set_platform_level(db, level,
                                    str(payload.get("reason") or ""),
                                    user["id"])
