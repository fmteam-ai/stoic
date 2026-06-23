"""Routes for the multi-agent activity log + FRED snapshot.

  GET /api/agents/activity       — latest 50 activity log entries for the user
  GET /api/agents/latest         — most recent activity entry per symbol
  GET /api/agents/macro          — current FRED macro snapshot (admin view)
"""
from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from database import get_db
from macro.fred import get_macro_snapshot

router = APIRouter(prefix="/agents", tags=["agents"])


def _serialise(doc: dict) -> dict:
    return {
        "id": str(doc.get("_id")),
        "user_id": doc.get("user_id"),
        "symbol": doc.get("symbol"),
        "tick_id": doc.get("tick_id"),
        "started_at": doc.get("started_at"),
        "completed_at": doc.get("completed_at"),
        "duration_ms": doc.get("duration_ms"),
        "steps": doc.get("steps") or [],
        "final_action": doc.get("final_action"),
        "final_confidence": doc.get("final_confidence"),
    }


@router.get("/activity")
async def list_activity(
    limit: int = Query(50, ge=1, le=200),
    symbol: str | None = None,
    user=Depends(get_current_user),
):
    db = get_db()
    q: dict = {"user_id": user["id"]}
    if symbol:
        q["symbol"] = symbol.upper()
    cursor = db.agent_activity.find(q).sort("started_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return {"items": [_serialise(d) for d in docs]}


@router.get("/latest")
async def latest_per_symbol(user=Depends(get_current_user)):
    """One latest entry per distinct symbol for the user."""
    db = get_db()
    pipeline = [
        {"$match": {"user_id": user["id"]}},
        {"$sort": {"started_at": -1}},
        {"$group": {"_id": "$symbol", "doc": {"$first": "$$ROOT"}}},
    ]
    out = []
    async for row in db.agent_activity.aggregate(pipeline):
        out.append(_serialise(row["doc"]))
    return {"items": out}


@router.get("/macro")
async def macro_snapshot(user=Depends(get_current_user)):  # noqa: ARG001 (auth gate only)
    snap = await get_macro_snapshot()
    return snap
