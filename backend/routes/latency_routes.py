"""T0→T9 latency profiler API — /api/latency"""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/latency", tags=["latency"])


def _uid(user):
    return None if user.get("role") == "admin" else user["id"]


@router.get("/summary")
async def latency_summary_ep(days: int = 7,
                             user=Depends(get_current_user)):
    from latency_profiler import latency_summary
    return await latency_summary(get_db(), days=days, user_id=_uid(user))


@router.get("/traces")
async def latency_traces_ep(limit: int = 30,
                            user=Depends(get_current_user)):
    from latency_profiler import recent_traces
    return {"traces": await recent_traces(get_db(), limit=limit,
                                          user_id=_uid(user))}
