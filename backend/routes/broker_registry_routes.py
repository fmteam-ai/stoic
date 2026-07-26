"""Broker registry admin CRUD + resolve endpoint (iter-139)."""
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user, require_admin
from database import get_db
from broker_servers import normalize_server
import broker_registry as reg

router = APIRouter(tags=["broker-registry"])


def _ser(b: dict) -> dict:
    b = dict(b)
    b["id"] = str(b.pop("_id"))
    return b


@router.get("/brokers/resolve")
async def resolve(server: str = "", user=Depends(get_current_user)):
    entry = await reg.resolve_registry(get_db(), server)
    return {"matched": bool(entry), "broker": entry}


@router.get("/admin/brokers")
async def list_brokers(user=Depends(get_current_user)):
    require_admin(user)
    db = get_db()
    return [_ser(b) async for b in db.broker_registry.find({}).sort("name", 1)]


def _validate(payload: dict) -> dict:
    out = {}
    for key in ("broker_id", "name"):
        v = (payload.get(key) or "").strip()
        if not v:
            raise HTTPException(status_code=400, detail=f"{key} required")
        out[key] = v[:80]
    aliases = payload.get("server_aliases") or []
    if not isinstance(aliases, list) or not aliases:
        raise HTTPException(status_code=400, detail="server_aliases list required")
    out["server_aliases"] = [str(a).strip()[:80] for a in aliases if str(a).strip()][:40]
    out["aliases_normalized"] = [normalize_server(a) for a in out["server_aliases"]]
    sm = payload.get("symbol_map") or {}
    if not isinstance(sm, dict):
        raise HTTPException(status_code=400, detail="symbol_map must be an object")
    out["symbol_map"] = {str(k).strip().upper()[:24]: str(v).strip()[:32]
                         for k, v in sm.items() if str(k).strip()}
    cs = payload.get("contract_specs") or {}
    if not isinstance(cs, dict):
        raise HTTPException(status_code=400, detail="contract_specs must be an object")
    out["contract_specs"] = cs
    for key in ("stop_level_points", "freeze_level_points"):
        try:
            out[key] = max(0, int(payload.get(key) or 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{key} must be an integer")
    sessions = payload.get("sessions") or []
    if not isinstance(sessions, list):
        raise HTTPException(status_code=400, detail="sessions must be a list")
    out["sessions"] = sessions[:10]
    out["updated_at"] = datetime.now(timezone.utc).isoformat()
    return out


@router.post("/admin/brokers")
async def create_broker(payload: dict, user=Depends(get_current_user)):
    require_admin(user)
    db = get_db()
    doc = _validate(payload)
    if await db.broker_registry.find_one({"broker_id": doc["broker_id"]}):
        raise HTTPException(status_code=409, detail="broker_id already exists")
    r = await db.broker_registry.insert_one(doc)
    reg.invalidate_cache()
    doc["_id"] = r.inserted_id
    return _ser(doc)


@router.put("/admin/brokers/{broker_id}")
async def update_broker(broker_id: str, payload: dict,
                        user=Depends(get_current_user)):
    require_admin(user)
    db = get_db()
    doc = _validate({**payload, "broker_id": broker_id})
    r = await db.broker_registry.update_one({"broker_id": broker_id},
                                            {"$set": doc})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="broker not found")
    reg.invalidate_cache()
    return _ser(await db.broker_registry.find_one({"broker_id": broker_id}))


@router.delete("/admin/brokers/{broker_id}")
async def delete_broker(broker_id: str, user=Depends(get_current_user)):
    require_admin(user)
    db = get_db()
    r = await db.broker_registry.delete_one({"broker_id": broker_id})
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail="broker not found")
    reg.invalidate_cache()
    return {"ok": True}
