"""Execution intent observability — /api/execution/*"""
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db
from modules.pamm.permissions import require_manager

router = APIRouter(prefix="/execution", tags=["execution"])


@router.get("/intents/stats")
async def intent_stats_ep(user=Depends(get_current_user)):
    db = get_db()
    await require_manager(db, user)
    by_status = {d["_id"]: d["n"] async for d in
                 db.execution_intents.aggregate(
                     [{"$group": {"_id": "$status", "n": {"$sum": 1}}}])}
    by_source = {d["_id"]: d["n"] async for d in
                 db.execution_intents.aggregate(
                     [{"$group": {"_id": "$source", "n": {"$sum": 1}}}])}
    return {"by_status": by_status, "by_source": by_source,
            "total": sum(by_status.values())}


@router.get("/intents")
async def list_intents_ep(status: str | None = None,
                          source: str | None = None,
                          program_id: str | None = None, limit: int = 50,
                          user=Depends(get_current_user)):
    db = get_db()
    await require_manager(db, user)
    q = {}
    if status:
        q["status"] = status
    if source:
        q["source"] = source
    if program_id:
        q["program_id"] = program_id
    lim = max(1, min(int(limit), 200))
    return {"intents": [i async for i in db.execution_intents.find(
        q, {"_id": 0}).sort("created_at", -1).limit(lim)]}


@router.get("/intents/{intent_id}")
async def intent_detail_ep(intent_id: str,
                           user=Depends(get_current_user)):
    db = get_db()
    await require_manager(db, user)
    doc = await db.execution_intents.find_one(
        {"intent_id": intent_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Intent not found")
    return doc
