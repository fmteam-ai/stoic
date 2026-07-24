"""Autopilot #10 — change governance API (approval queue + ledger)."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/governance", tags=["governance"])


@router.get("/policy")
async def governance_policy(user=Depends(get_current_user)):
    from change_governance import (FORBIDDEN_FIELDS, SAFER_BOOL,
                                   SAFER_DIRECTION)
    return {"principle": "the bot may automatically become more conservative;"
                         " becoming more aggressive requires approval",
            "safer_direction": SAFER_DIRECTION,
            "safer_bool": SAFER_BOOL,
            "forbidden_fields": sorted(FORBIDDEN_FIELDS)}


@router.get("/changes")
async def governed_changes(status: str | None = None, limit: int = 50,
                           user=Depends(get_current_user)):
    db = get_db()
    q = {"user_id": user["id"]}
    if status:
        q["status"] = status
    rows = []
    async for r in db.governed_changes.find(q).sort(
            "proposed_at", -1).limit(max(1, min(limit, 200))):
        r["id"] = str(r.pop("_id"))
        rows.append(r)
    pending = await db.governed_changes.count_documents(
        {"user_id": user["id"], "status": "pending"})
    return {"changes": rows, "pending_count": pending}


class ProposeBody(BaseModel):
    field: str
    new_value: float | int | bool | str
    account_id: str | None = None
    evidence: str | None = None


@router.post("/propose")
async def propose(body: ProposeBody, user=Depends(get_current_user)):
    from change_governance import propose_change
    db = get_db()
    q = {"user_id": user["id"], "active": True}
    if body.account_id:
        q["account_id"] = body.account_id
    cfg = await db.bot_configs.find_one(q) or {}
    doc = await propose_change(db, user["id"], body.field,
                               cfg.get(body.field), body.new_value,
                               source="manual_proposal",
                               evidence=body.evidence,
                               account_id=body.account_id)
    doc["id"] = str(doc.pop("_id"))
    return doc


@router.post("/changes/{change_id}/approve")
async def approve_change(change_id: str, user=Depends(get_current_user)):
    from change_governance import resolve_change
    return await resolve_change(get_db(), user["id"], change_id, approve=True)


@router.post("/changes/{change_id}/reject")
async def reject_change(change_id: str, user=Depends(get_current_user)):
    from change_governance import resolve_change
    return await resolve_change(get_db(), user["id"], change_id,
                                approve=False)
