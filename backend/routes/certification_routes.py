"""Certification API — /api/certification (system vs strategy split,
tiered strategy certification, issued certs with expiry + revocation)."""
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/certification", tags=["certification"])


async def _owned_account(db, user, account_id: str) -> dict:
    from route_utils import parse_object_id
    q = {"_id": parse_object_id(account_id)}
    if user.get("role") != "admin":
        q["user_id"] = user["id"]
    acc = await db.accounts.find_one(q)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    return acc


@router.get("/system")
async def system_certification_ep(account_id: str,
                                  user=Depends(get_current_user)):
    from certification import system_certification
    db = get_db()
    acc = await _owned_account(db, user, account_id)
    return await system_certification(db, acc)


@router.get("/strategy")
async def strategy_certification_ep(scope: str = Query("ai"),
                                    user=Depends(get_current_user)):
    from certification import strategy_certification
    return await strategy_certification(get_db(), user["id"], scope)


@router.post("/issue")
async def issue_certification_ep(payload: dict,
                                 user=Depends(get_current_user)):
    """Compute the certification NOW and persist it with an expiry
    (system 7d, strategy 30d)."""
    from certification import (issue, strategy_certification,
                               system_certification)
    db = get_db()
    kind = str(payload.get("kind") or "")
    if kind == "system":
        acc = await _owned_account(db, user,
                                   str(payload.get("account_id") or ""))
        result = await system_certification(db, acc)
    elif kind == "strategy":
        result = await strategy_certification(
            db, user["id"], str(payload.get("scope") or "ai"))
    else:
        raise HTTPException(status_code=400,
                            detail="kind must be system|strategy")
    return await issue(db, user["id"], result)


@router.get("/active")
async def active_certifications_ep(user=Depends(get_current_user)):
    from certification import active
    return {"certifications": await active(
        get_db(), user["id"], admin=user.get("role") == "admin")}


@router.post("/revoke")
async def revoke_certification_ep(payload: dict,
                                  user=Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    from certification import revoke
    cert_id = str(payload.get("cert_id") or "")
    if not cert_id:
        raise HTTPException(status_code=400, detail="cert_id required")
    out = await revoke(get_db(), cert_id, user["id"],
                       str(payload.get("reason") or ""))
    if out.get("error"):
        raise HTTPException(status_code=404, detail=out["error"])
    return out
