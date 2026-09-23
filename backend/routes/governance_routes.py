"""Autopilot #10 — change governance API (approval queue + ledger).

Hardening: approvals of risk-increasing changes require fresh step-up MFA
(TOTP), a written reason, and produce immutable config-version snapshots +
append-only audit events. Material changes need dual approval."""
from fastapi import APIRouter, Depends, Request, HTTPException
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


class ApproveBody(BaseModel):
    reason: str


class RejectBody(BaseModel):
    reason: str | None = None


@router.post("/changes/{change_id}/approve")
async def approve_change(change_id: str, body: ApproveBody, request: Request,
                         user=Depends(get_current_user)):
    from change_governance import resolve_change
    from step_up import audit_event, require_step_up
    db = get_db()
    # risk-increasing approval = live-sensitive → fresh TOTP required
    await require_step_up(db, user, request, "risk_raise")
    result = await resolve_change(db, user["id"], change_id, approve=True,
                                  reason=body.reason)
    await audit_event(db, user["id"], "governance_approve",
                      {"change_id": change_id, "reason": body.reason,
                       "result": result.get("status") or result.get("error")},
                      request, step_up=True)
    return result


@router.post("/changes/{change_id}/reject")
async def reject_change(change_id: str, request: Request,
                        body: RejectBody | None = None,
                        user=Depends(get_current_user)):
    from change_governance import resolve_change
    from step_up import audit_event
    db = get_db()
    result = await resolve_change(db, user["id"], change_id, approve=False,
                                  reason=(body.reason if body else None))
    await audit_event(db, user["id"], "governance_reject",
                      {"change_id": change_id,
                       "result": result.get("status") or result.get("error")},
                      request)
    if result.get("error") == "change not found":
        raise HTTPException(status_code=404, detail="change not found")
    return result
