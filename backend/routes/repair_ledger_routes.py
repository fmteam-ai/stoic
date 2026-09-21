"""Admin · Repair Ledger — read-only view over `repair_ledger`, the immutable
record every automated repair writes (health_repairs.py). Each row carries a
correlation id (one per sweep), the repair kind, the owning user and the
exact affected record ids, so any state mutation can be traced back.

    GET /api/admin/repair-ledger?limit=&skip=&kind=&user_id=&correlation_id=&source=
    GET /api/admin/repair-ledger/kinds
    GET /api/admin/repair-ledger/{correlation_id}
"""
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user, require_admin
from database import get_db

router = APIRouter(tags=["admin"])

MAX_LIMIT = 500


def _clean(d: dict) -> dict:
    d["id"] = str(d.pop("_id"))
    d.setdefault("affected_ids", [])
    d.setdefault("count", len(d["affected_ids"]))
    return d


async def _emails(db, user_ids: set) -> dict:
    if not user_ids:
        return {}
    from bson import ObjectId
    from bson.errors import InvalidId
    oids = []
    for uid in user_ids:
        try:
            oids.append(ObjectId(uid))
        except (InvalidId, TypeError):
            pass
    rows = await db.users.find({"_id": {"$in": oids}}, {"email": 1}).to_list(length=len(oids))
    return {str(r["_id"]): r.get("email") for r in rows}


def _filters(kind: str, user_id: str, correlation_id: str, source: str) -> dict:
    q = {}
    if kind:
        q["kind"] = kind
    if user_id:
        q["user_id"] = user_id
    if correlation_id:
        q["correlation_id"] = correlation_id
    if source:
        q["source"] = source
    return q


@router.get("/admin/repair-ledger")
async def list_repair_ledger(limit: int = 100, skip: int = 0, kind: str = "",
                             user_id: str = "", correlation_id: str = "",
                             source: str = "", user=Depends(get_current_user)):
    require_admin(user)
    db = get_db()
    limit = min(max(limit, 1), MAX_LIMIT)
    skip = max(skip, 0)
    q = _filters(kind, user_id, correlation_id, source)
    total = await db.repair_ledger.count_documents(q)
    rows = [_clean(d) for d in await db.repair_ledger.find(q).sort("at", -1)
            .skip(skip).limit(limit).to_list(length=limit)]
    emails = await _emails(db, {r.get("user_id") for r in rows if r.get("user_id")})
    for r in rows:
        r["user_email"] = emails.get(r.get("user_id"))
    return {"rows": rows, "total": total, "limit": limit, "skip": skip, "filters": q}


@router.get("/admin/repair-ledger/kinds")
async def repair_ledger_kinds(user=Depends(get_current_user)):
    require_admin(user)
    db = get_db()
    pipeline = [{"$group": {"_id": "$kind", "sweeps": {"$sum": 1},
                            "records": {"$sum": {"$ifNull": ["$count", 0]}},
                            "last_at": {"$max": "$at"}}},
                {"$sort": {"sweeps": -1}}]
    kinds = [{"kind": k["_id"], "sweeps": k["sweeps"], "records": k["records"],
              "last_at": k["last_at"]} async for k in db.repair_ledger.aggregate(pipeline)]
    sources = await db.repair_ledger.distinct("source")
    total_rows = await db.repair_ledger.count_documents({})
    return {"kinds": kinds, "sources": sorted(s for s in sources if s),
            "total_rows": total_rows,
            "total_records": sum(k["records"] for k in kinds)}


@router.get("/admin/repair-ledger/{correlation_id}")
async def repair_ledger_sweep(correlation_id: str, user=Depends(get_current_user)):
    require_admin(user)
    db = get_db()
    rows = [_clean(d) for d in await db.repair_ledger.find(
        {"correlation_id": correlation_id}).sort("at", 1).to_list(length=MAX_LIMIT)]
    if not rows:
        raise HTTPException(status_code=404, detail="correlation id not found")
    emails = await _emails(db, {r.get("user_id") for r in rows if r.get("user_id")})
    for r in rows:
        r["user_email"] = emails.get(r.get("user_id"))
    return {"correlation_id": correlation_id, "rows": rows,
            "user_id": rows[0].get("user_id"), "user_email": rows[0].get("user_email"),
            "source": rows[0].get("source"), "at": rows[0].get("at"),
            "records": sum(r["count"] for r in rows)}
