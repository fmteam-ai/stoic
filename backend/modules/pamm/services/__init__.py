"""PAMM program lifecycle services — thin orchestration over the broker
adapter; the broker owns money, STOIC owns strategy/risk/reporting."""
import uuid
from datetime import datetime, timezone

from services.broker_gateway.pamm_api import SANDBOX_PARTNER_ID, get_adapter
from modules.pamm.events import emit_event


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_program(db, name: str, manager_id: str,
                         partner_id: str = SANDBOX_PARTNER_ID,
                         currency: str = "USD",
                         manager_fee_pct: float = 20.0) -> dict:
    adapter = await get_adapter(db, partner_id)
    if partner_id == SANDBOX_PARTNER_ID:
        broker_prog = await adapter.ensure_program(name, currency)
    else:  # real brokers: program must already exist broker-side
        progs = await adapter.get_pamm_programs()
        broker_prog = next((p for p in progs if p.get("name") == name), None)
        if not broker_prog:
            raise ValueError("program not found on broker")
    doc = {"program_id": f"pgm_{uuid.uuid4().hex[:10]}",
           "broker_program_id": broker_prog["program_id"],
           "partner_id": partner_id, "name": name[:120],
           "currency": currency, "manager_id": manager_id,
           "manager_fee_pct": float(manager_fee_pct),
           "status": "active", "trading": "enabled",
           "emergency_stop": False, "investor_count": 0,
           "aum": broker_prog.get("nav"), "last_nav": None,
           "created_at": _now()}
    await db.pamm_programs.insert_one(dict(doc))
    await db.pamm_master_accounts.insert_one(
        {"program_id": doc["program_id"],
         "broker_login": broker_prog.get("master_login"),
         "partner_id": partner_id, "created_at": _now()})
    await emit_event(db, "ProgramCreated",
                     {"program_id": doc["program_id"], "name": name,
                      "manager_id": manager_id, "partner_id": partner_id})
    doc.pop("_id", None)
    return doc


async def get_program(db, program_id: str) -> dict | None:
    return await db.pamm_programs.find_one(
        {"program_id": program_id}, {"_id": 0})


async def list_programs(db, manager_id: str | None = None) -> list:
    q = {"manager_id": manager_id} if manager_id else {}
    return [p async for p in
            db.pamm_programs.find(q, {"_id": 0}).sort("created_at", -1)]


async def add_investor(db, program: dict, investor: dict,
                       amount: float) -> dict:
    """Broker performs onboarding + allocation; STOIC mirrors the result."""
    adapter = await get_adapter(db, program["partner_id"])
    inv = await adapter.create_investor(program["broker_program_id"],
                                        investor)
    alloc = await adapter.allocate(program["broker_program_id"],
                                   inv["investor_id"], float(amount))
    await db.pamm_allocations.insert_one(
        {"program_id": program["program_id"],
         "investor_id": inv["investor_id"],
         "broker_allocation_id": alloc["allocation_id"],
         "amount": float(amount), "at": _now()})
    await emit_event(db, "InvestorCreated",
                     {"program_id": program["program_id"],
                      "investor_id": inv["investor_id"]})
    await emit_event(db, "AllocationUpdated",
                     {"program_id": program["program_id"],
                      "investor_id": inv["investor_id"],
                      "amount": float(amount)})
    return {"investor": inv, "allocation": alloc}


async def nav_history(db, program_id: str, limit: int = 100) -> list:
    return [n async for n in db.pamm_nav_snapshots
            .find({"program_id": program_id}, {"_id": 0})
            .sort("at", -1).limit(limit)]
