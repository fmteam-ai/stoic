"""STOIC Connect API (iter-154) — Stripe-simple MT5 onboarding.

  POST /api/connect/start                 — create account + pairing token + one install command
  GET  /api/connect/{account_id}/status   — whole pipeline in plain language
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/connect", tags=["stoic-connect"])


@router.post("/start")
async def connect_start(payload: dict, request: Request,
                        user=Depends(get_current_user)):
    from connect_service import start_connect
    for f in ("broker", "server", "account_number"):
        if not str(payload.get(f) or "").strip():
            raise HTTPException(status_code=422, detail=f"{f} is required")
    return await start_connect(get_db(), user, payload, request)


@router.get("/{account_id}/status")
async def connect_status_ep(account_id: str,
                            user=Depends(get_current_user)):
    from connect_service import connect_status
    from route_utils import parse_object_id
    db = get_db()
    q = {"_id": parse_object_id(account_id, "Account")}
    if user.get("role") != "admin":
        q["user_id"] = user["id"]
    acc = await db.accounts.find_one(q)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    return await connect_status(db, acc)
