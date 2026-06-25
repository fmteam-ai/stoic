"""Shadow Performance routes — surfaces the hypothetical track-record of
signals emitted by `paper_shadow_mode` bots.

  GET /api/shadow/performance?since_days=90&limit=500
"""
from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from database import get_db
from shadow_performance import compute_aggregate

router = APIRouter(prefix="/shadow", tags=["shadow"])


@router.get("/performance")
async def shadow_performance(since_days: int = Query(90, ge=1, le=365),
                             limit: int = Query(500, ge=1, le=2000),
                             user=Depends(get_current_user)):
    db = get_db()
    return await compute_aggregate(db, user["id"], since_days=since_days, limit=limit)
