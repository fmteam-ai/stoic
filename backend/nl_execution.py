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
  effects    the deterministic idempotency key owns a row in `nl_effects` that is a
             fenced STATE MACHINE (r16 P0-01): reserved → dispatching → applying →
             completed|failed. Immediately before every effect the worker re-checks
             fence + live lease, that the proposal is still executing (not cancelled)
             and the CURRENT canonical authority version; the ultimate handler calls
             apply_effect() right before its domain write and stamps the mutated rows
             with {key, fence, execution_id, decision_id, authority_version}. A stale
             worker fails the CAS and writes nothing. Recovery consults the row:
             not yet applying → re-run under the new fence; completed → copy result;
             applying → uncertain, never replayed.
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
    return {"_id": doc["_id"], "status": "executing", "execution.id": ex["id"], "execution.owner": ex["owner"],
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


EFFECT_FINAL = ("completed", "failed", "uncertain")


def effect_context(doc: dict, key: str, authority: dict | None) -> dict:
    """Everything an ultimate handler / outbox event needs to enforce the fence."""
    ex = doc["execution"]
    return {"idempotency_key": key, "execution_id": ex["id"], "owner": ex["owner"], "fence": int(ex["fence"]),
            "decision_id": (authority or {}).get("decision_id"),
            "authority_version": (authority or {}).get("input_version")}


async def reserve_effect(db, idem_key: str, meta: dict | None = None, *, ctx: dict | None = None) -> str:
    """Effect state machine entry: reserved → dispatching → applying → completed|failed.
    Returns the state this worker now holds the row in: 'reserved' (fresh or
    taken over from a stale owner that never reached the write), 'completed' /
    'failed' (a previous owner finished — result is on the row), 'uncertain'
    (a previous owner may be mid-write — never replayed), or 'lost'."""
    ctx = ctx or {}
    base = {"state": "reserved", "owner": ctx.get("owner"), "fence": int(ctx.get("fence") or 0),
            "execution_id": ctx.get("execution_id"), "authority_version": ctx.get("authority_version"),
            "decision_id": ctx.get("decision_id"), "at": _now().isoformat(), **(meta or {})}
    try:
        await db.nl_effects.insert_one({"_id": idem_key, **base})
        return "reserved"
    except DuplicateKeyError:
        pass
    row = await db.nl_effects.find_one({"_id": idem_key})
    if not row:
        return "lost"
    if row.get("state") in EFFECT_FINAL:
        return row["state"]
    if row.get("state") == "applying":
        # a stale owner may be inside the domain write — fence it so its
        # completion fails and record the outcome as uncertain (audit r16 P0-01)
        await db.nl_effects.update_one({"_id": idem_key, "state": "applying", "fence": {"$lt": base["fence"]}},
                                       {"$set": {"state": "uncertain", "fenced_by": base["owner"],
                                                 "fence": base["fence"], "uncertain_at": _now().isoformat()}})
        return "uncertain"
    # reserved/dispatching by a lower fence: take it over — that owner has not
    # written anything yet and its apply CAS will now fail
    res = await db.nl_effects.find_one_and_update(
        {"_id": idem_key, "state": {"$in": ["reserved", "dispatching"]}, "fence": {"$lt": base["fence"]}},
        {"$set": {**base, "taken_over_from": row.get("owner")}}, return_document=ReturnDocument.AFTER)
    return "reserved" if res else "lost"


async def dispatch_effect(db, ctx: dict) -> bool:
    """reserved → dispatching (fenced). Called right before the handler."""
    res = await db.nl_effects.update_one(
        {"_id": ctx["idempotency_key"], "state": "reserved", "owner": ctx["owner"], "fence": ctx["fence"]},
        {"$set": {"state": "dispatching", "dispatched_at": _now().isoformat()}})
    return res.matched_count > 0


async def apply_effect(db, ctx: dict | None) -> None:
    """THE side-effect boundary — every ultimate handler calls this immediately
    before its domain write: dispatching → applying under (owner, fence).
    A stale worker fails here and raises LeaseLost, so it writes nothing."""
    if not ctx:
        return
    res = await db.nl_effects.update_one(
        {"_id": ctx["idempotency_key"], "state": "dispatching", "owner": ctx["owner"], "fence": ctx["fence"]},
        {"$set": {"state": "applying", "applying_at": _now().isoformat()}})
    if res.matched_count == 0:
        raise LeaseLost("effect fenced out before the side effect")


def effect_stamp(ctx: dict | None) -> dict:
    """Fields stamped on every mutated domain row / outbox event so downstream
    consumers (EA bridge, reconciler) can dedupe by key and reject stale fences."""
    if not ctx:
        return {}
    return {"nl_effect": {"key": ctx["idempotency_key"], "fence": ctx["fence"],
                          "execution_id": ctx["execution_id"], "decision_id": ctx.get("decision_id"),
                          "authority_version": ctx.get("authority_version")}}


async def complete_effect(db, ctx: dict, state: str, result=None, error: str | None = None) -> bool:
    res = await db.nl_effects.update_one(
        {"_id": ctx["idempotency_key"], "state": {"$in": ["reserved", "dispatching", "applying"]},
         "owner": ctx["owner"], "fence": ctx["fence"]},
        {"$set": {"state": state, "result": result, "error": error, "completed_at": _now().isoformat()}})
    return res.matched_count > 0


async def recheck_authority(db, user_id: str, act: dict, authority: dict | None) -> tuple[dict | None, str | None]:
    """Fresh canonical authority immediately before an effect. Risk-increasing
    actions are refused when exposure is no longer allowed OR the authority
    input version moved since the operator confirmed."""
    if authority is None:
        return None, None
    from canonical_decision import decide_user
    fresh = await decide_user(db, user_id, fresh=True)
    if is_risk_increasing(act):
        if not fresh.get("new_exposure_allowed", True):
            return fresh, f"authority {fresh.get('state')} — risk-increasing action refused"
        if authority.get("input_version") is not None and fresh.get("input_version") != authority.get("input_version"):
            return fresh, "authority inputs changed since confirmation — risk-increasing action refused"
    return fresh, None


async def _write_receipt(db, coll: str, doc: dict, key: str, rec: dict) -> bool:
    res = await db[coll].update_one(_guard(doc), {"$set": {f"action_receipts.{key}": rec}})
    return res.matched_count > 0


async def _recover_started(db, coll: str, doc: dict, key: str, base: dict, user_id: str, authority):
    """A `started` receipt from a previous owner: consult the effect row.
    Nothing reached the write → we may run it under our fence; finished →
    copy the outcome; mid-write → uncertain, never replayed."""
    ctx = effect_context(doc, key, authority)
    state = await reserve_effect(db, key, {"user_id": user_id, "type": base["type"]}, ctx=ctx)
    now = _now().isoformat()
    if state == "reserved":
        return None, ctx
    row = await db.nl_effects.find_one({"_id": key}) or {}
    if state == "completed":
        return {**base, "state": "done", "result": row.get("result"), "recovered": True, "at": now}, None
    if state == "failed":
        return {**base, "state": "failed", "error": row.get("error") or "action_failed", "recovered": True, "at": now}, None
    return {**base, "state": "uncertain",
            "error": "crashed_mid_action — effect may have applied; not replayed (capital safety)", "at": now}, None


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
        ctx = None
        if prev and prev.get("state") == "started":
            rec, ctx = await _recover_started(db, coll, doc, key, base, user_id, authority)
            if rec is not None:
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
            if ctx is None:
                ctx = effect_context(doc, key, authority)
                state = await reserve_effect(db, key, {"user_id": user_id, "type": a_type}, ctx=ctx)
                if state != "reserved":
                    rec = {**base, "state": "uncertain", "error": f"effect_key_already_{state} — not replayed",
                           "at": _now().isoformat()}
                    ctx = None
            if ctx is not None:
                # audit r16 P0-01 — immediately before the effect: fence + live lease,
                # proposal not cancelled, and CURRENT canonical authority
                await renew_lease(db, coll, doc)
                fresh, refusal = await recheck_authority(db, user_id, act, authority)
                if refusal:
                    await complete_effect(db, ctx, "failed", error=refusal)
                    rec = {**base, "state": "suppressed", "reason": refusal,
                           "decision_id": (fresh or {}).get("decision_id"), "at": _now().isoformat()}
                elif not await dispatch_effect(db, ctx):
                    raise LeaseLost("effect fenced out at dispatch")
                else:
                    if fresh is not None:
                        ctx = {**ctx, "decision_id": fresh.get("decision_id"),
                               "authority_version": fresh.get("input_version")}
                    try:
                        result = await execute_one(user_id, act, idem_key=key, ctx=ctx)
                    except LeaseLost:
                        raise
                    except Exception as e:  # noqa: BLE001
                        await complete_effect(db, ctx, "failed", error=f"action_failed: {type(e).__name__}")
                        raise
                    if not await complete_effect(db, ctx, "completed", result=result):
                        raise LeaseLost("effect fenced out at completion")
                    rec = {**base, "state": "done", "result": result, "decision_id": ctx.get("decision_id"),
                           "authority_version": ctx.get("authority_version"), "at": _now().isoformat()}
        except LeaseLost:
            lost = True
            break
        except Exception as e:  # noqa: BLE001 — receipt must record the failure
            rec = {**base, "state": "failed", "error": f"action_failed: {type(e).__name__}",
                   "at": _now().isoformat()}
        if not await _write_receipt(db, coll, doc, key, rec):
            # we are fenced out: the recovering owner resolves this action from
            # the effect row — we must not touch anything else.
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
