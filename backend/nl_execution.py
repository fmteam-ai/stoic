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


async def fenced(db, ctx: dict | None, work):
    """THE side-effect boundary (audit r17 P0-01). Runs `work(session)` so that
    the effect-row assertion (`applying` under THIS owner+fence), the domain
    mutation and the transition to `completed` commit in ONE MongoDB
    transaction — a stale worker that resumes after a takeover aborts without
    writing anything. Without a replica set (preview/CI) the fallback is the
    CAS assertion immediately before the writes; production REQUIRES
    transactions (fail closed, nothing is written)."""
    if not ctx:
        return await work(None)
    from pymongo.errors import OperationFailure
    owned = {"_id": ctx["idempotency_key"], "owner": ctx["owner"], "fence": ctx["fence"]}
    dispatching = {**owned, "state": "dispatching"}
    applying = {**owned, "state": "applying"}
    now = _now().isoformat()

    async def _run(session):
        res = await db.nl_effects.update_one(dispatching, {"$set": {"state": "applying", "applying_at": now}},
                                             session=session)
        if res.matched_count == 0:
            raise LeaseLost("effect fenced out before the side effect")
        result = await work(session)
        res = await db.nl_effects.update_one(
            applying, {"$set": {"state": "completed", "result": result, "completed_at": _now().isoformat(),
                                "committed": "transaction" if session is not None else "cas"}}, session=session)
        if res.matched_count == 0:
            raise LeaseLost("effect fenced out at commit")
        return result

    client = db.client
    try:
        async with await client.start_session() as s:
            async with s.start_transaction():
                return await _run(s)
    except OperationFailure as e:
        if "Transaction numbers" not in str(e) and getattr(e, "code", None) not in (20, 263):
            raise
        if await transactions_required(db):
            raise RuntimeError("NL effects require a replica-set MongoDB (transactions) whenever capital "
                               "can be touched — standalone fallback only under NL_EFFECTS_SYNTHETIC_ONLY=true "
                               "with zero live-enabled accounts")
    # synthetic-only standalone fallback (CI): the same CAS sequence without atomic commit
    return await _run(None)


def close_command_update(reason: str, ctx: dict | None, **stamp) -> dict:
    """Mongo update that writes the current close command AND atomically
    increments the trade's DURABLE close_seq (r18 P0-01). The terminal orders
    commands by close_seq across proposals/PANIC/recovery and dedupes
    close_idem_key; the proposal-local fence is recorded for backend audit only.
    The previous unresolved command is kept as a supersession record."""
    ctx = ctx or {}
    now = _now().isoformat()
    return {
        "$inc": {"close_seq": 1},
        "$set": {"close_requested": True, "close_reason": reason,
                 "close_idem_key": ctx.get("idempotency_key"), "close_fence": ctx.get("fence"),
                 "close_command": {"key": ctx.get("idempotency_key"), "fence": ctx.get("fence"),
                                   "execution_id": ctx.get("execution_id"), "reason": reason,
                                   "state": "requested", "requested_at": now}, **stamp},
        "$push": {"close_command_history": {"$each": [{"key": ctx.get("idempotency_key"), "reason": reason,
                                                       "requested_at": now}], "$slice": -20}},
    }


async def transactions_required(db) -> bool:
    """r18 P1-01: atomic commit is required whenever capital can be touched —
    production, OR any live-mode account with trading enabled, OR broker execution
    enabled — regardless of APP_ENV. Standalone fallback is permitted only under an
    explicit, verified synthetic-only posture (NL_EFFECTS_SYNTHETIC_ONLY=true AND
    zero live-enabled accounts)."""
    from app_env import is_production
    if is_production():
        return True
    if await capital_capable(db):
        return True
    return (os.environ.get("NL_EFFECTS_SYNTHETIC_ONLY") or "").lower() != "true"


async def capital_capable(db) -> bool:
    """r20 P1-02: ANY representation of live capital counts — a trading-enabled
    account whose mode is missing/empty/any non-paper value (missing defaults to
    live elsewhere), or any account with a terminal binding (bridge token /
    heartbeat) or broker credentials."""
    live_like = {"trading_enabled": True, "status": {"$ne": "deleted"},
                 "$nor": [{"mode": {"$regex": "^paper$", "$options": "i"}}]}
    if await db.accounts.count_documents(live_like):
        return True
    bound = {"status": {"$ne": "deleted"}, "$or": [
        {"bridge_token": {"$exists": True, "$nin": [None, ""]}},
        {"last_heartbeat": {"$exists": True, "$nin": [None, ""]}},
        {"broker_password_enc": {"$exists": True, "$nin": [None, ""]}},
        {"mt5_password_enc": {"$exists": True, "$nin": [None, ""]}}]}
    return bool(await db.accounts.count_documents(bound))


def target_filter(user_id: str, ctx: dict | None, fallback: dict) -> dict:
    """Immutable approved targets (r17 P1-01): mutate ONLY the ids bound to the
    effect at confirmation; the caller's token-derived query is used only
    when no target list was bound (never for a recovered execution)."""
    ids = (ctx or {}).get("target_ids")
    if ids is None:
        return fallback
    from bson import ObjectId
    oids = [ObjectId(i) for i in ids if ObjectId.is_valid(i)]
    return {"user_id": user_id, "_id": {"$in": oids}}


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


NO_TARGET_BINDING = {"PANIC_LOCK", "SET_CONDITIONAL_TRIGGER"}   # account-wide / creates a row


def base_action(doc: dict, index: int) -> dict:
    acts = doc.get("actions") or doc.get("then") or []
    return acts[index] if index < len(acts) else {}


async def bound_targets(db, doc: dict, index: int, user_id: str, act: dict):
    """Immutable target ids for action `index` (r17 P1-01): the APPROVED preview's
    resolved_ids when the document carries a preview (proposals); otherwise
    (triggers fire without an operator preview) resolved ONCE now and persisted
    on the effect row so recovery never re-resolves a broad token."""
    a_type = str(act.get("type") or "").upper()
    if a_type in NO_TARGET_BINDING:
        return None
    items = ((doc.get("preview") or {}).get("actions") or [])
    if index < len(items) and items[index].get("resolved_ids") is not None:
        ids = items[index]["resolved_ids"]
    else:
        from nl_preview import preview_action
        ids = (await preview_action(db, user_id, act)).get("resolved_ids") or []
    return [str(i).split(":", 1)[0] for i in ids]


async def _write_receipt(db, coll: str, doc: dict, key: str, rec: dict) -> bool:
    res = await db[coll].update_one(_guard(doc), {"$set": {f"action_receipts.{key}": rec}})
    return res.matched_count > 0


async def _recover_started(db, coll: str, doc: dict, key: str, base: dict, user_id: str, authority):
    """A `started` receipt from a previous owner: consult the effect row.
    Nothing reached the write → we may run it under our fence; finished →
    copy the outcome; mid-write → uncertain, never replayed."""
    ctx = effect_context(doc, key, authority)
    prior = await db.nl_effects.find_one({"_id": key}, {"target_ids": 1})
    ctx["target_ids"] = (prior or {}).get("target_ids")
    if ctx["target_ids"] is None:
        ctx["target_ids"] = await bound_targets(db, doc, base["index"], user_id, base_action(doc, base["index"]))
    state = await reserve_effect(db, key, {"user_id": user_id, "type": base["type"],
                                           "target_ids": ctx["target_ids"]}, ctx=ctx)
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
                ctx["target_ids"] = await bound_targets(db, doc, i, user_id, act)
                state = await reserve_effect(db, key, {"user_id": user_id, "type": a_type,
                                                       "target_ids": ctx["target_ids"]}, ctx=ctx)
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
                        # the handler commits its writes + `completed` atomically via fenced()
                        result = await execute_one(user_id, act, idem_key=key, ctx=ctx)
                    except LeaseLost:
                        raise
                    except Exception as e:  # noqa: BLE001
                        await complete_effect(db, ctx, "failed", error=f"action_failed: {type(e).__name__}")
                        raise
                    row = await db.nl_effects.find_one({"_id": key}, {"state": 1, "owner": 1, "fence": 1})
                    if not row or row.get("owner") != ctx["owner"] or row.get("fence") != ctx["fence"]:
                        raise LeaseLost("effect fenced out at completion")
                    if row.get("state") != "completed" and not await complete_effect(db, ctx, "completed", result=result):
                        raise LeaseLost("effect fenced out at completion")
                    rec = {**base, "state": "done", "result": result, "decision_id": ctx.get("decision_id"),
                           "authority_version": ctx.get("authority_version"),
                           "target_ids": ctx.get("target_ids"), "at": _now().isoformat()}
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
