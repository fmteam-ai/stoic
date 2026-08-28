"""Soak campaign + broker-attached validation API — /api/ops/soak
(admin-only: this is production-proof machinery, not tenant data)."""
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/ops", tags=["soak"])


def _require_admin(user):
    if (user or {}).get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")


@router.post("/soak/start")
async def soak_start_ep(payload: dict | None = None,
                        user=Depends(get_current_user)):
    _require_admin(user)
    from soak_campaign import start
    p = payload or {}
    return await start(get_db(), user["id"],
                       days=int(p.get("days") or 14),
                       account_id=p.get("account_id"),
                       note=p.get("note"))


@router.post("/soak/checkpoint")
async def soak_checkpoint_ep(user=Depends(get_current_user)):
    _require_admin(user)
    from soak_campaign import record_checkpoint
    return await record_checkpoint(get_db())


@router.post("/soak/incident")
async def soak_incident_ep(payload: dict,
                           user=Depends(get_current_user)):
    _require_admin(user)
    from soak_campaign import log_incident
    sev = str(payload.get("severity") or "").lower()
    if sev not in ("critical", "major", "minor"):
        raise HTTPException(status_code=400,
                            detail="severity must be critical|major|minor")
    return await log_incident(get_db(), sev,
                              str(payload.get("note") or ""), user["id"])


@router.get("/soak/status")
async def soak_status_ep(user=Depends(get_current_user)):
    _require_admin(user)
    from soak_campaign import status
    return await status(get_db())


@router.get("/soak/evidence")
async def soak_evidence_ep(campaign_id: str | None = None,
                           user=Depends(get_current_user)):
    """Immutable hash-chained Production Evidence records."""
    _require_admin(user)
    from soak_campaign import evidence
    return await evidence(get_db(), campaign_id)


@router.get("/broker-validation")
async def broker_validation_ep(account_id: str,
                               user=Depends(get_current_user)):
    _require_admin(user)
    from route_utils import parse_object_id
    from soak_campaign import broker_validation
    db = get_db()
    acc = await db.accounts.find_one({"_id": parse_object_id(account_id)})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    return await broker_validation(db, acc)
