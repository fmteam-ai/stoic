"""Loss post-mortem API — fetch investigations and toggle auto-tightening.

  GET    /api/postmortem                         — paginated list
  GET    /api/postmortem/patterns                — aggregate by pattern_key
  GET    /api/postmortem/{trade_id}              — single investigation
  POST   /api/postmortem/{trade_id}/regenerate   — manual re-run
  GET    /api/postmortem/settings                — `{auto_tighten_enabled}`
  POST   /api/postmortem/settings                — toggle
  GET    /api/postmortem/adjustments             — history of auto-tightens
"""
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from auth import get_current_user
from database import get_db
from route_utils import parse_object_id
from loss_postmortem import maybe_record_postmortem, _user_doc

router = APIRouter(prefix="/postmortem", tags=["postmortem"])


def _serialize(d: dict) -> dict:
    return {
        "id": str(d.get("_id")),
        "trade_id": d.get("trade_id"),
        "symbol": d.get("symbol"),
        "action": d.get("action"),
        "pnl": d.get("pnl"),
        "close_reason": d.get("close_reason"),
        "account_id": d.get("account_id"),
        "pattern_key": d.get("pattern_key"),
        "trigger": d.get("trigger"),
        "diff": d.get("diff") or {},
        "narrative": d.get("narrative") or {},
        "adjustment": d.get("adjustment"),
        "created_at": d.get("created_at"),
    }


# ---- settings (placed BEFORE /{trade_id} to avoid path-param shadowing) ----
@router.get("/settings")
async def get_settings(user=Depends(get_current_user)):
    db = get_db()
    u = await _user_doc(db, user["id"]) or {}
    s = u.get("postmortem_settings") or {}
    return {
        "auto_tighten_enabled": bool(s.get("auto_tighten_enabled", False)),
        "updated_at": s.get("updated_at"),
    }


@router.post("/settings")
async def set_settings(payload: dict, user=Depends(get_current_user)):
    db = get_db()
    u = await _user_doc(db, user["id"])
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    enabled = bool(payload.get("auto_tighten_enabled"))
    await db.users.update_one(
        {"_id": u["_id"]},
        {"$set": {"postmortem_settings": {
            "auto_tighten_enabled": enabled,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }}},
    )
    return {"auto_tighten_enabled": enabled}


# ---- adjustments audit trail ----
@router.get("/adjustments")
async def list_adjustments(limit: int = 50, user=Depends(get_current_user)):
    db = get_db()
    cur = db.guardrail_adjustments.find({"user_id": user["id"]}).sort("created_at", -1).limit(min(limit, 200))
    docs = await cur.to_list(length=200)
    return {"items": [
        {**{k: v for k, v in d.items() if k != "_id"}, "id": str(d["_id"])}
        for d in docs
    ]}


# ---- list / patterns / single ----
@router.get("/patterns")
async def list_patterns(days: int = 30, user=Depends(get_current_user)):
    """Aggregate post-mortems by `pattern_key` so the user can spot the
    recurring losing setups at a glance. Returns top patterns by count.
    """
    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    pipeline = [
        {"$match": {"user_id": user["id"], "created_at": {"$gte": cutoff}}},
        {"$group": {
            "_id": "$pattern_key",
            "count": {"$sum": 1},
            "total_pnl": {"$sum": "$pnl"},
            "last_at": {"$max": "$created_at"},
            "sample_trade_id": {"$last": "$trade_id"},
            "last_narrative": {"$last": "$narrative.summary"},
            "last_suggestion": {"$last": "$narrative.suggested_guardrail"},
        }},
        {"$sort": {"count": -1}},
        {"$limit": 25},
    ]
    out = []
    async for row in db.loss_postmortems.aggregate(pipeline):
        out.append({
            "pattern_key": row["_id"],
            "count": row["count"],
            "total_pnl": round(float(row.get("total_pnl") or 0), 2),
            "last_at": row.get("last_at"),
            "sample_trade_id": row.get("sample_trade_id"),
            "last_narrative": row.get("last_narrative"),
            "last_suggestion": row.get("last_suggestion"),
        })
    return {"items": out, "window_days": days}


@router.get("")
async def list_postmortems(limit: int = 50, pattern_key: Optional[str] = None,
                            user=Depends(get_current_user)):
    db = get_db()
    q = {"user_id": user["id"]}
    if pattern_key:
        q["pattern_key"] = pattern_key
    cur = db.loss_postmortems.find(q).sort("created_at", -1).limit(min(limit, 200))
    docs = await cur.to_list(length=200)
    return {"items": [_serialize(d) for d in docs]}


@router.get("/{trade_id}")
async def get_postmortem(trade_id: str, user=Depends(get_current_user)):
    db = get_db()
    # Verify trade ownership first
    trade = await db.trades.find_one({
        "_id": parse_object_id(trade_id, "Trade"),
        "user_id": user["id"],
    })
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    doc = await db.loss_postmortems.find_one({"trade_id": trade_id})
    if not doc:
        return {"exists": False, "trade_id": trade_id}
    return {"exists": True, **_serialize(doc)}


@router.post("/{trade_id}/regenerate")
async def regenerate(trade_id: str, user=Depends(get_current_user)):
    """Force-rerun the post-mortem investigation for a closed losing trade.

    Useful when the original LLM call failed or the user wants a refreshed
    market-condition diff. Deletes the existing record (if any) and inserts
    a new one.
    """
    db = get_db()
    trade = await db.trades.find_one({
        "_id": parse_object_id(trade_id, "Trade"),
        "user_id": user["id"],
    })
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.get("status") != "closed":
        raise HTTPException(status_code=400, detail="Trade is not closed yet")
    if float(trade.get("pnl") or 0) >= 0:
        raise HTTPException(status_code=400, detail="Trade was not a loss")
    await db.loss_postmortems.delete_many({"trade_id": trade_id})
    doc = await maybe_record_postmortem(db, trade["_id"])
    if not doc:
        raise HTTPException(status_code=400, detail="Trade is not eligible for post-mortem")
    return _serialize(doc)
