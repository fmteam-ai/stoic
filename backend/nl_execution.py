"""Exactly-once, FENCED execution of proposed NL / trigger action sets
(audit r14 P0-01/P0-02, r15 P0-01).

Contract shared by Risk Commander confirmations (`nl_proposals`) and conditional
triggers (`conditional_triggers`):
  claim      pending/active → executing via ONE find_one_and_update — execution id,
             owner, integer FENCE token (incremented on every claim/reclaim) and lease.
  guard      every receipt transition, action start, lease renewal and finalization
             is bound to (execution.id, owner, fence, live lease). A stale worker that
             resumes after a reclaim fails the guard and stops BEFORE its next side
             effect; it can never overwrite a recovered receipt.
  effects    the deterministic idempotency key is reserved in `nl_effects` (unique)
             right before the side effect, so even a stale worker that slipped past
             the document guard is rejected at the side-effect boundary.
  finalize   executed | partially_executed | failed — same fenced guard.
  recover    an expired lease is re-claimed (fence+1); actions with a final receipt
             are never replayed; a `started` receipt (crash mid-action) → `uncertain`.
"""
import hashlib
import json
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

LEASE_SEC = 120
RISK_INCREASING = {"ENABLE_BOTS"}
FINAL_STATES = ("done", "failed", "suppressed", "uncertain")


class LeaseLost(RuntimeError):
    """Raised when the fenced guard no longer matches — the worker is stale."""


def _now():
    return datetime.now(timezone.utc)


def owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def is_risk_increasing(act: dict) -> bool:
    a_type = str(act.get("type") or "").upper()
    if a_type in RISK_INCREASING:
        return True
    if a_type == "SET_RISK_LEVEL":
        return str((act.get("params") or {}).get("risk_level") or "low").lower() != "low"
    # arming a trigger touches nothing; its `then` actions are re-checked against
    # authority AT FIRE TIME (trigger_sweeper.fire_claimed)
    return False


def idempotency_key(execution_id: str, index: int, act: dict) -> str:
    canon = json.dumps({"type": str(act.get("type") or "").upper(),
                        "target": act.get("target") or "all",
                        "params": act.get("params") or {}}, sort_keys=True)
    return hashlib.sha256(f"{execution_id}:{index}:{canon}".encode()).hexdigest()[:32]


def _guard(doc: dict) -> dict:
    ex = doc["execution"]
    return {"_id": doc["_id"], "execution.id": ex["id"], "execution.owner": ex["owner"],
            "execution.fence": ex["fence"], "execution.lease_until": {"$gt": _now().isoformat()}}


async def claim(db, coll: str, filt: dict, *, from_status, extra_set: dict | None = None):
    """CAS claim. Returns the claimed document or None when somebody else won."""
    now = _now()
    prev_fence = 0
    execution = {"id": uuid.uuid4().hex, "owner": owner_id(), "attempt": 1, "fence": prev_fence + 1,
                 "claimed_at": now.isoformat(),
                 "lease_until": (now + timedelta(seconds=LEASE_SEC)).isoformat()}
    return await db[coll].find_one_and_update(
        {**filt, "status": from_status},
        {"$set": {"status": "executing", "execution": execution, **(extra_set or {})}},
        return_document=ReturnDocument.AFTER)


async def reclaim_expired(db, coll: str, doc: dict):
    """Re-claim an `executing` doc whose lease expired: new owner, fence+1."""
    ex = doc.get("execution") or {}
    if doc.get("status") != "executing" or not ex:
        return None
    now = _now()
    if datetime.fromisoformat(ex["lease_until"]) > now:
        return None
    return await db[coll].find_one_and_update(
        {"_id": doc["_id"], "status": "executing", "execution.id": ex["id"],
         "execution.fence": ex.get("fence", 1), "execution.lease_until": ex["lease_until"]},
        {"$set": {"execution.owner": owner_id(),
                  "execution.attempt": int(ex.get("attempt") or 1) + 1,
                  "execution.lease_until": (now + timedelta(seconds=LEASE_SEC)).isoformat(),
                  "execution.reclaimed_at": now.isoformat()},
         "$inc": {"execution.fence": 1}},
        return_document=ReturnDocument.AFTER)


async def renew_lease(db, coll: str, doc: dict) -> None:
    """Extend the lease under the fenced guard; LeaseLost if we are stale."""
    res = await db[coll].update_one(
        _guard(doc), {"$set": {"execution.lease_until": (_now() + timedelta(seconds=LEASE_SEC)).isoformat()}})
    if res.matched_count == 0:
        raise LeaseLost("lease lost or fenced out")


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


async def reserve_effect(db, idem_key: str, meta: dict | None = None) -> bool:
    """Ultimate side-effect boundary: one idempotency key ⇒ one effect, ever."""
    try:
        await db.nl_effects.insert_one({"_id": idem_key, "at": _now().isoformat(), **(meta or {})})
        return True
    except DuplicateKeyError:
        return False


async def _write_receipt(db, coll: str, doc: dict, key: str, rec: dict) -> bool:
    res = await db[coll].update_one(_guard(doc), {"$set": {f"action_receipts.{key}": rec}})
    return res.matched_count > 0


async def run_claimed(db, coll: str, doc: dict, user_id: str, actions: list,
                      *, authority: dict | None = None, execute_one=None) -> dict:
    """Execute `actions` under the fenced claim held in `doc`. Idempotent per key."""
    if execute_one is None:
        from routes.nl_routes import execute_one as _exec
        execute_one = _exec
    ex_id = doc["execution"]["id"]
    stored = doc.get("action_receipts") or {}
    receipts = []
    lost = False
    for i, act in enumerate(actions):
        key = idempotency_key(ex_id, i, act)
        prev = stored.get(key)
        a_type = str(act.get("type") or "").upper()
        base = {"key": key, "index": i, "type": a_type, "target": act.get("target") or "all",
                "fence": doc["execution"]["fence"]}
        if prev and prev.get("state") in FINAL_STATES:
            receipts.append(prev)
            continue
        if prev and prev.get("state") == "started":
            rec = {**base, "state": "uncertain",
                   "error": "crashed_mid_action — not replayed (capital safety)",
                   "at": _now().isoformat()}
            if not await _write_receipt(db, coll, doc, key, rec):
                lost = True
                break
            receipts.append(rec)
            continue
        if authority is not None and not authority.get("new_exposure_allowed", True) and is_risk_increasing(act):
            rec = {**base, "state": "suppressed",
                   "reason": f"authority {authority.get('state')} — risk-increasing action refused",
                   "decision_id": authority.get("decision_id"), "at": _now().isoformat()}
            if not await _write_receipt(db, coll, doc, key, rec):
                lost = True
                break
            receipts.append(rec)
            continue
        # renew the lease and record `started` — both fenced; a stale worker stops HERE
        try:
            await renew_lease(db, coll, doc)
        except LeaseLost:
            lost = True
            break
        if not await _write_receipt(db, coll, doc, key, {**base, "state": "started", "at": _now().isoformat()}):
            lost = True
            break
        try:
            if not await reserve_effect(db, key, {"user_id": user_id, "type": a_type, "execution_id": ex_id}):
                rec = {**base, "state": "uncertain", "error": "effect_key_already_reserved — not replayed",
                       "at": _now().isoformat()}
            else:
                result = await execute_one(user_id, act, idem_key=key)
                rec = {**base, "state": "done", "result": result, "at": _now().isoformat()}
        except Exception as e:  # noqa: BLE001 — receipt must record the failure
            rec = {**base, "state": "failed", "error": f"action_failed: {type(e).__name__}",
                   "at": _now().isoformat()}
        if not await _write_receipt(db, coll, doc, key, rec):
            # side effect happened but we are fenced out: the recovering owner
            # sees `started` and marks it uncertain — we must not touch anything else.
            lost = True
            break
        receipts.append(rec)
    if lost:
        return {"status": "lease_lost", "receipts": receipts, "execution_id": ex_id,
                "fence": doc["execution"]["fence"]}
    status = finalize_status(receipts)
    res = await db[coll].update_one(
        {**_guard(doc), "status": "executing"},
        {"$set": {"status": status, "receipts": receipts, "finished_at": _now().isoformat()}})
    if res.matched_count == 0:
        return {"status": "lease_lost", "receipts": receipts, "execution_id": ex_id,
                "fence": doc["execution"]["fence"]}
    return {"status": status, "receipts": receipts, "execution_id": ex_id, "fence": doc["execution"]["fence"]}


def pending_actions(doc: dict, actions: list) -> list:
    """Indices whose receipt is not final (used by recovery/tests)."""
    ex_id = (doc.get("execution") or {}).get("id") or ""
    stored = doc.get("action_receipts") or {}
    return [i for i, a in enumerate(actions)
            if (stored.get(idempotency_key(ex_id, i, a)) or {}).get("state") not in FINAL_STATES]
