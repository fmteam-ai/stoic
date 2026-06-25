"""Strategies CRUD — save / list / delete user-compiled NL strategies.

The compile (Claude → JSON) and backtest endpoints already live under
`/api/nl/strategy` and `/api/nl/strategy/backtest`. This module just adds
persistence so the /strategies page can show a user's library.

Schema for db.strategies:
  {
    "_id": ObjectId,
    "user_id": str,
    "name": str,
    "prompt": str,            # original NL prompt
    "compiled": dict,         # full Claude output (symbols, risk_level, ...)
    "backtest": dict | None,  # optional last-run backtest result
    "created_at": iso,
    "updated_at": iso,
  }
"""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db
from route_utils import parse_object_id

router = APIRouter(prefix="/strategies", tags=["strategies"])


def _serialise(doc: dict) -> dict:
    return {
        "id": str(doc.get("_id")),
        "name": doc.get("name"),
        "prompt": doc.get("prompt"),
        "compiled": doc.get("compiled") or {},
        "dsl": doc.get("dsl"),
        "backtest": doc.get("backtest"),
        "optimization": doc.get("optimization"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


@router.get("")
async def list_strategies(user=Depends(get_current_user)):
    db = get_db()
    cursor = db.strategies.find({"user_id": user["id"]}).sort("created_at", -1).limit(50)
    docs = await cursor.to_list(length=50)
    return {"items": [_serialise(d) for d in docs]}


@router.post("")
async def save_strategy(payload: dict, user=Depends(get_current_user)):
    name = (payload.get("name") or "").strip()
    compiled = payload.get("compiled") or {}
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if len(name) > 80:
        raise HTTPException(status_code=400, detail="name too long (max 80)")
    if not compiled or compiled.get("clarification_needed"):
        raise HTTPException(status_code=400, detail="No usable compiled strategy")

    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "user_id": user["id"],
        "name": name,
        "prompt": (payload.get("prompt") or "")[:2000],
        "compiled": compiled,
        "dsl": payload.get("dsl"),
        "backtest": payload.get("backtest"),
        "optimization": payload.get("optimization"),
        "created_at": now,
        "updated_at": now,
    }
    db = get_db()
    res = await db.strategies.insert_one(doc)
    doc["_id"] = res.inserted_id
    return _serialise(doc)


@router.delete("/{strategy_id}")
async def delete_strategy(strategy_id: str, user=Depends(get_current_user)):
    db = get_db()
    res = await db.strategies.delete_one({
        "_id": parse_object_id(strategy_id, "Strategy"),
        "user_id": user["id"],
    })
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="strategy not found")
    return {"ok": True, "id": strategy_id}
