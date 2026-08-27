"""Verdict Outcome Tracking API — /api/verdicts"""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/verdicts", tags=["verdicts"])


def _uid(user):
    return None if user.get("role") == "admin" else user["id"]


@router.get("/effectiveness")
async def verdict_effectiveness_ep(days: int = 30,
                                   user=Depends(get_current_user)):
    from verdict_tracking import effectiveness_summary
    return await effectiveness_summary(get_db(), days=days,
                                       user_id=_uid(user))


@router.get("/recent")
async def verdict_recent_ep(limit: int = 50,
                            user=Depends(get_current_user)):
    db = get_db()
    q = {} if user.get("role") == "admin" else {"user_id": user["id"]}
    lim = max(1, min(int(limit), 200))
    return {"verdicts": [v async for v in db.risk_verdicts.find(
        q, {"_id": 0}).sort("created_at", -1).limit(lim)]}
