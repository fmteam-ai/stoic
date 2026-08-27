"""PAMM Strategy Marketplace (Phases 7-8) — published program listings +
investor join-request funnel. The broker still owns money: an approved
request creates the allocation broker-side via the adapter."""
import uuid
from datetime import datetime, timezone

from modules.pamm.events import emit_event

LISTING_FIELDS = {"_id": 0, "program_id": 1, "name": 1, "currency": 1,
                  "manager_fee_pct": 1, "investor_count": 1, "aum": 1,
                  "pitch": 1, "published_at": 1, "trading": 1}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def set_published(db, program: dict, publish: bool,
                        pitch: str | None = None) -> dict:
    upd = {"published": bool(publish)}
    if publish:
        upd["published_at"] = _now()
    if pitch is not None:
        upd["pitch"] = str(pitch)[:500]
    await db.pamm_programs.update_one(
        {"program_id": program["program_id"]}, {"$set": upd})
    return {"program_id": program["program_id"], "published": bool(publish)}


async def public_listings(db) -> list:
    from modules.pamm.reports import performance_summary
    out = []
    async for p in db.pamm_programs.find(
            {"published": True, "status": "active"},
            dict(LISTING_FIELDS)).sort("published_at", -1):
        perf = await performance_summary(db, p["program_id"])
        out.append({**p, "performance": {
            k: perf.get(k) for k in ("total_return_pct", "max_drawdown_pct",
                                     "points", "from", "to")}})
    return out


async def create_join_request(db, program: dict, user: dict, amount: float,
                              note: str = "") -> dict:
    if not (amount and amount > 0):
        raise ValueError("amount must be positive")
    pending = await db.pamm_join_requests.find_one(
        {"program_id": program["program_id"], "user_id": user["id"],
         "status": "pending"}, {"_id": 1})
    if pending:
        raise ValueError("You already have a pending request for this program")
    doc = {"request_id": f"jrq_{uuid.uuid4().hex[:10]}",
           "program_id": program["program_id"],
           "program_name": program.get("name"),
           "user_id": user["id"], "email": user.get("email"),
           "name": user.get("name") or user.get("email"),
           "amount": float(amount), "note": str(note or "")[:300],
           "status": "pending", "at": _now()}
    await db.pamm_join_requests.insert_one(dict(doc))
    await emit_event(db, "JoinRequested",
                     {"program_id": program["program_id"],
                      "request_id": doc["request_id"],
                      "amount": doc["amount"]})
    await db.pamm_notifications.insert_one(
        {"type": "JoinRequested", "program_id": program["program_id"],
         "at": _now(), "seen": False,
         "summary": f"Join request for {program.get('name')}: "
                    f"{doc['amount']:,.2f} from {doc['email']}"})
    doc.pop("_id", None)
    return doc


async def my_requests(db, user_id: str) -> list:
    return [r async for r in db.pamm_join_requests.find(
        {"user_id": user_id}, {"_id": 0}).sort("at", -1).limit(50)]


async def list_join_requests(db, program_id: str) -> list:
    return [r async for r in db.pamm_join_requests.find(
        {"program_id": program_id}, {"_id": 0}).sort("at", -1).limit(200)]


async def _notify_requester(db, req: dict, approved: bool) -> None:
    await db.notifications.insert_one({
        "user_id": req["user_id"],
        "kind": "pamm_join_" + ("approved" if approved else "rejected"),
        "title": ("Your managed-strategy request was approved"
                  if approved else "Your managed-strategy request was declined"),
        "message": f"Program: {req.get('program_name')} · "
                   f"amount {req['amount']:,.2f}",
        "severity": "info" if approved else "warning",
        "read": False, "created_at": _now()})


async def decide_join_request(db, request_id: str, approve: bool,
                              actor_id: str) -> dict:
    # atomic claim so a double-click can't approve twice
    req = await db.pamm_join_requests.find_one_and_update(
        {"request_id": request_id, "status": "pending"},
        {"$set": {"status": "processing"}})
    if not req:
        raise ValueError("request not found or already decided")
    req.pop("_id", None)
    program = await db.pamm_programs.find_one(
        {"program_id": req["program_id"]}, {"_id": 0})
    if not program:
        await db.pamm_join_requests.update_one(
            {"request_id": request_id}, {"$set": {"status": "pending"}})
        raise ValueError("program no longer exists")
    if approve:
        try:
            from modules.pamm.services import add_investor
            result = await add_investor(
                db, program, {"name": req.get("name"),
                              "email": req.get("email")}, req["amount"])
        except Exception:
            await db.pamm_join_requests.update_one(
                {"request_id": request_id}, {"$set": {"status": "pending"}})
            raise
        await db.pamm_join_requests.update_one(
            {"request_id": request_id},
            {"$set": {"status": "approved", "decided_at": _now(),
                      "decided_by": actor_id,
                      "investor_id": result["investor"]["investor_id"]}})
        # keep the marketplace card fresh; reconcile overwrites with broker truth
        await db.pamm_programs.update_one(
            {"program_id": req["program_id"]},
            {"$inc": {"investor_count": 1}})
        await emit_event(db, "JoinApproved",
                         {"program_id": req["program_id"],
                          "request_id": request_id,
                          "investor_id": result["investor"]["investor_id"],
                          "amount": req["amount"]})
    else:
        await db.pamm_join_requests.update_one(
            {"request_id": request_id},
            {"$set": {"status": "rejected", "decided_at": _now(),
                      "decided_by": actor_id}})
        await emit_event(db, "JoinRejected",
                         {"program_id": req["program_id"],
                          "request_id": request_id})
    await _notify_requester(db, req, approve)
    return {"request_id": request_id,
            "status": "approved" if approve else "rejected"}
