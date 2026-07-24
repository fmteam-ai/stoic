"""Autopilot #7/#8 — learning records + failure taxonomy API."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/learning", tags=["learning"])


@router.get("/records")
async def learning_records(days: int = 30, category: str | None = None,
                           limit: int = 50,
                           user=Depends(get_current_user)):
    from datetime import datetime, timedelta, timezone
    db = get_db()
    since = (datetime.now(timezone.utc)
             - timedelta(days=max(1, min(days, 365)))).isoformat()
    q = {"user_id": user["id"], "closed_at": {"$gte": since}}
    if category:
        q["failure.category"] = category
    rows = []
    async for r in db.learning_records.find(q).sort(
            "closed_at", -1).limit(max(1, min(limit, 200))):
        r["id"] = str(r.pop("_id"))
        rows.append(r)
    return {"records": rows, "count": len(rows)}


@router.get("/failure-summary")
async def failure_taxonomy(days: int = 30, user=Depends(get_current_user)):
    from learning_record import failure_summary
    db = get_db()
    return await failure_summary(db, user["id"],
                                 days=max(1, min(days, 365)))
