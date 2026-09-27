"""Exactly-once execution of proposed NL / trigger action sets (audit r14 P0-01/P0-02).

Contract shared by Risk Commander confirmations (`nl_proposals`) and conditional
triggers (`conditional_triggers`):
  claim      pending/active → executing via ONE find_one_and_update (execution id,
             owner, lease) — concurrent callers lose the CAS and never execute.
  execute    every action has a deterministic idempotency key; a durable receipt
             is written BEFORE (started) and AFTER (done/failed/suppressed) each
             action; risk-increasing actions are suppressed while canonical
             authority is not READY for new exposure.
  finalize   executed | partially_executed | failed — CAS-guarded on execution id.
  recover    an expired lease is re-claimed; actions with a final receipt are never
             replayed; an action found `started` (crash mid-action) is marked
             `uncertain` and NOT replayed — a capital action never runs twice.
"""
import hashlib
import json
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

LEASE_SEC = 120
RISK_INCREASING = {"ENABLE_BOTS"}
FINAL_STATES = ("done", "failed", "suppressed", "uncertain")


def _now():
    return datetime.now(timezone.utc)


def owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def is_risk_increasing(act: dict) -> bool:
    a_type = str(act.get("type") or "").upper()
    if a_type in RISK_INCREASING:
        return True
    if a_type == "SET_RISK_LEVEL":
        return str((act.get("params") or {}).get("risk_level") or "low").lower() != "low"
    return False


def idempotency_key(execution_id: str, index: int, act: dict) -> str:
    canon = json.dumps({"type": str(act.get("type") or "").upper(),
                        "target": act.get("target") or "all",
                        "params": act.get("params") or {}}, sort_keys=True)
    return hashlib.sha256(f"{execution_id}:{index}:{canon}".encode()).hexdigest()[:32]


async def claim(db, coll: str, filt: dict, *, from_status: str, extra_set: dict | None = None):
    """CAS claim. Returns the claimed document or None when somebody else won."""
    now = _now()
    execution = {"id": uuid.uuid4().hex, "owner": owner_id(), "attempt": 1,
                 "claimed_at": now.isoformat(),
                 "lease_until": (now + timedelta(seconds=LEASE_SEC)).isoformat()}
    return await db[coll].find_one_and_update(
        {**filt, "status": from_status},
        {"$set": {"status": "executing", "execution": execution, **(extra_set or {})}},
        return_document=ReturnDocument.AFTER)


async def reclaim_expired(db, coll: str, doc: dict):
    """Re-claim an `executing` doc whose lease expired. None if still leased or won by another."""
    ex = doc.get("execution") or {}
    if doc.get("status") != "executing" or not ex:
        return None
    now = _now()
    if datetime.fromisoformat(ex["lease_until"]) > now:
        return None
    return await db[coll].find_one_and_update(
        {"_id": doc["_id"], "status": "executing", "execution.id": ex["id"],
         "execution.lease_until": ex["lease_until"]},
        {"$set": {"execution.owner": owner_id(),
                  "execution.attempt": int(ex.get("attempt") or 1) + 1,
                  "execution.lease_until": (now + timedelta(seconds=LEASE_SEC)).isoformat(),
                  "execution.reclaimed_at": now.isoformat()}},
        return_document=ReturnDocument.AFTER)


def lease_live(doc: dict) -> bool:
    ex = doc.get("execution") or {}
    return bool(ex) and datetime.fromisoformat(ex["lease_until"]) > _now()


def finalize_status(receipts: list) -> str:
    states = [r.get("state") for r in receipts]
    if receipts and all(s == "done" for s in states):
        return "executed"
    if any(s == "done" for s in states):
        return "partially_executed"
    return "failed"


async def run_claimed(db, coll: str, doc: dict, user_id: str, actions: list,
                      *, authority: dict | None = None, execute_one=None) -> dict:
    """Execute `actions` under the claim held in `doc`. Idempotent per key."""
    if execute_one is None:
        from routes.nl_routes import execute_one as _exec
        execute_one = _exec
    ex_id = doc["execution"]["id"]
    guard = {"_id": doc["_id"], "execution.id": ex_id}
    stored = doc.get("action_receipts") or {}
    receipts = []
    for i, act in enumerate(actions):
        key = idempotency_key(ex_id, i, act)
        prev = stored.get(key)
        a_type = str(act.get("type") or "").upper()
        base = {"key": key, "index": i, "type": a_type, "target": act.get("target") or "all"}
        if prev and prev.get("state") in FINAL_STATES:
            receipts.append(prev)
            continue
        if prev and prev.get("state") == "started":
            rec = {**base, "state": "uncertain",
                   "error": "crashed_mid_action — not replayed (capital safety)",
                   "at": _now().isoformat()}
            await db[coll].update_one(guard, {"$set": {f"action_receipts.{key}": rec}})
            receipts.append(rec)
            continue
        if authority is not None and not authority.get("new_exposure_allowed", True) and is_risk_increasing(act):
            rec = {**base, "state": "suppressed",
                   "reason": f"authority {authority.get('state')} — risk-increasing action refused",
                   "decision_id": authority.get("decision_id"), "at": _now().isoformat()}
            await db[coll].update_one(guard, {"$set": {f"action_receipts.{key}": rec}})
            receipts.append(rec)
            continue
        started = await db[coll].update_one(
            guard, {"$set": {f"action_receipts.{key}": {**base, "state": "started",
                                                        "at": _now().isoformat()}}})
        if started.matched_count == 0:
            # lost the lease (re-claimed elsewhere) — stop without acting
            receipts.append({**base, "state": "failed", "error": "lease_lost"})
            break
        try:
            result = await execute_one(user_id, act)
            rec = {**base, "state": "done", "result": result, "at": _now().isoformat()}
        except Exception as e:  # noqa: BLE001 — receipt must record the failure
            rec = {**base, "state": "failed", "error": f"action_failed: {type(e).__name__}",
                   "at": _now().isoformat()}
        await db[coll].update_one(guard, {"$set": {f"action_receipts.{key}": rec}})
        receipts.append(rec)
    status = finalize_status(receipts)
    await db[coll].update_one(
        {**guard, "status": "executing"},
        {"$set": {"status": status, "receipts": receipts,
                  "finished_at": _now().isoformat()}})
    return {"status": status, "receipts": receipts, "execution_id": ex_id}


def pending_actions(doc: dict, actions: list) -> list:
    """Indices whose receipt is not final (used by recovery/tests)."""
    ex_id = (doc.get("execution") or {}).get("id") or ""
    stored = doc.get("action_receipts") or {}
    return [i for i, a in enumerate(actions)
            if (stored.get(idempotency_key(ex_id, i, a)) or {}).get("state") not in FINAL_STATES]
