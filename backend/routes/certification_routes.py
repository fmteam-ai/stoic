"""Certification API — /api/certification (system vs strategy split)."""
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
