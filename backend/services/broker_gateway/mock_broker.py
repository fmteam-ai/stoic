"""Local mock broker (v55 §1) — a real HTTP broker implementing the
standard STOIC PAMM REST contract, mounted at /api/mockbroker/*. The
RestBrokerAdapter certifies against it over genuine HTTP round-trips.
State lives in its OWN collections (mockbroker_*) so it is a truly
independent source of truth. Auth: bearer key (hash stored server-side)."""
import hashlib
import random
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from database import get_db

router = APIRouter(prefix="/mockbroker", tags=["mockbroker"])

REST_DEMO_PARTNER_ID = "prt_rest_demo"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _auth(request: Request) -> None:
    tok = (request.headers.get("authorization") or "")
    tok = tok[7:].strip() if tok.lower().startswith("bearer ") else ""
    cfg = await get_db().mockbroker_config.find_one({"_id": "auth"})
    if (not tok or not cfg or hashlib.sha256(tok.encode()).hexdigest()
            != cfg.get("key_hash")):
        raise HTTPException(status_code=401, detail="unauthorized")


async def _program(pid: str) -> dict:
    p = await get_db().mockbroker_programs.find_one(
        {"program_id": pid}, {"_id": 0})
    if not p:
        raise HTTPException(status_code=404, detail="unknown program")
    return p


@router.get("/programs")
async def list_programs(request: Request):
    await _auth(request)
    return [p async for p in get_db().mockbroker_programs.find(
        {}, {"_id": 0})]


@router.post("/programs")
async def create_program(payload: dict, request: Request):
    """Broker back-office: provision a PAMM program."""
    await _auth(request)
    doc = {"program_id": f"mbx_{uuid.uuid4().hex[:10]}",
           "name": str(payload.get("name") or "program")[:120],
           "currency": str(payload.get("currency") or "USD"),
           "status": "active", "trading": "enabled",
           "nav": float(payload.get("initial_nav") or 100000.0),
           "master_login": f"7{random.randint(1000000, 9999999)}",
           "created_at": _now()}
    await get_db().mockbroker_programs.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


@router.get("/programs/{pid}/master")
async def master(pid: str, request: Request):
    await _auth(request)
    p = await _program(pid)
    investors = await get_db().mockbroker_investors.count_documents(
        {"program_id": pid, "status": "active"})
    return {"program_id": pid, "login": p["master_login"],
            "master_login": p["master_login"], "currency": p["currency"],
            "equity": p["nav"], "balance": p["nav"],
            "margin_level": 100.0, "investor_count": investors,
            "trading": p["trading"]}


@router.get("/programs/{pid}/nav")
async def nav(pid: str, request: Request):
    await _auth(request)
    p = await _program(pid)
    drifted = round(p["nav"] * (1 + random.uniform(-0.0012, 0.0015)), 2)
    await get_db().mockbroker_programs.update_one(
        {"program_id": pid}, {"$set": {"nav": drifted}})
    return {"program_id": pid, "nav": drifted, "currency": p["currency"],
            "at": _now()}


@router.get("/programs/{pid}/positions")
async def positions(pid: str, request: Request):
    await _auth(request)
    await _program(pid)
    return [p async for p in get_db().mockbroker_positions.find(
        {"program_id": pid}, {"_id": 0})]


@router.post("/programs/{pid}/positions")
async def add_position(pid: str, payload: dict, request: Request):
    """Broker back-office: open a position on the master (drift drills)."""
    await _auth(request)
    await _program(pid)
    doc = {"position_id": f"mpx_{uuid.uuid4().hex[:8]}", "program_id": pid,
           "symbol": str(payload.get("symbol") or "XAUUSD"),
           "volume": float(payload.get("volume") or 0.1),
           "side": str(payload.get("side") or "BUY"), "at": _now()}
    await get_db().mockbroker_positions.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


@router.post("/programs/{pid}/investors")
async def create_investor(pid: str, payload: dict, request: Request):
    await _auth(request)
    await _program(pid)
    doc = {"investor_id": f"mbi_{uuid.uuid4().hex[:10]}",
           "program_id": pid,
           "name": str(payload.get("name") or "investor")[:80],
           "email": str(payload.get("email") or "")[:120],
           "balance": 0.0, "status": "active", "created_at": _now()}
    await get_db().mockbroker_investors.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


@router.post("/programs/{pid}/allocations")
async def allocate(pid: str, payload: dict, request: Request):
    await _auth(request)
    await _program(pid)
    db = get_db()
    inv = await db.mockbroker_investors.find_one(
        {"investor_id": str(payload.get("investor_id")),
         "program_id": pid})
    if not inv:
        raise HTTPException(status_code=404, detail="unknown investor")
    amount = float(payload.get("amount") or 0)
    if amount <= 0:
        raise HTTPException(status_code=400,
                            detail="allocation must be positive")
    await db.mockbroker_investors.update_one(
        {"_id": inv["_id"]}, {"$inc": {"balance": amount}})
    await db.mockbroker_programs.update_one(
        {"program_id": pid}, {"$inc": {"nav": amount}})
    doc = {"allocation_id": f"mba_{uuid.uuid4().hex[:10]}",
           "program_id": pid, "investor_id": inv["investor_id"],
           "amount": amount, "at": _now()}
    await db.mockbroker_allocations.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


@router.post("/programs/{pid}/pause")
async def pause(pid: str, request: Request):
    await _auth(request)
    await _program(pid)
    await get_db().mockbroker_programs.update_one(
        {"program_id": pid}, {"$set": {"trading": "paused"}})
    return {"program_id": pid, "trading": "paused"}


@router.post("/programs/{pid}/resume")
async def resume(pid: str, request: Request):
    await _auth(request)
    await _program(pid)
    await get_db().mockbroker_programs.update_one(
        {"program_id": pid}, {"$set": {"trading": "enabled"}})
    return {"program_id": pid, "trading": "enabled"}


@router.post("/programs/{pid}/close-all")
async def close_all(pid: str, request: Request):
    await _auth(request)
    await _program(pid)
    r = await get_db().mockbroker_positions.delete_many(
        {"program_id": pid})
    return {"program_id": pid, "closed": r.deleted_count}


async def ensure_rest_demo_partner(db) -> dict:
    """Idempotent seed: a REST demo partner whose adapter points at this
    mock broker via genuine HTTP (localhost:8001 internal)."""
    existing = await db.broker_partners.find_one(
        {"partner_id": REST_DEMO_PARTNER_ID}, {"_id": 0})
    if existing:
        return existing
    import secrets_vault
    key = uuid.uuid4().hex + uuid.uuid4().hex
    await db.mockbroker_config.update_one(
        {"_id": "auth"},
        {"$set": {"key_hash": hashlib.sha256(key.encode()).hexdigest(),
                  "rotated_at": _now()}}, upsert=True)
    doc = {"partner_id": REST_DEMO_PARTNER_ID, "name": "REST Demo Broker",
           "adapter": "rest", "status": "active",
           "webhook_secret_enc": secrets_vault.encrypt(
               uuid.uuid4().hex + uuid.uuid4().hex,
               associated_data=b"broker_webhook_secret"),
           "rest_config": {
               "base_url": "http://localhost:8001/api/mockbroker",
               "api_key_enc": secrets_vault.encrypt(
                   key, associated_data=b"broker_api_key")},
           "created_at": _now()}
    await db.broker_partners.update_one(
        {"partner_id": REST_DEMO_PARTNER_ID}, {"$setOnInsert": doc},
        upsert=True)
    if not await db.mockbroker_programs.find_one({}):
        await db.mockbroker_programs.insert_one(
            {"program_id": f"mbx_{uuid.uuid4().hex[:10]}",
             "name": "REST Demo Growth Fund", "currency": "USD",
             "status": "active", "trading": "enabled", "nav": 250000.0,
             "master_login": f"7{random.randint(1000000, 9999999)}",
             "created_at": _now()})
    doc.pop("_id", None)
    return doc
