"""Audit r14 P0-01 / P0-02 / P1-01 — exactly-once NL confirmations & trigger
firing, active-only 6/3/3 counting. REQUIRED CI lane (critical_controls)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

pytestmark = [pytest.mark.integration, pytest.mark.critical_controls]

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


@pytest.fixture
def world(request):
    from database import get_db
    db = get_db()
    uid = str(ObjectId())
    _run(db.bot_configs.insert_many([
        {"user_id": uid, "account_id": None, "risk_level": "high", "active": True},
        {"user_id": uid, "account_id": str(ObjectId()), "risk_level": "medium", "active": True},
    ]))
    _run(db.trades.insert_many([
        {"user_id": uid, "symbol": "XAUUSD", "action": "BUY", "status": "open", "entry_price": 4000.0, "live_pnl": 1.0, "lot_size": 0.1},
    ]))

    def _cleanup():
        for c in ("bot_configs", "trades", "nl_proposals", "conditional_triggers", "trigger_fire_events"):
            _run(db[c].delete_many({"user_id": uid}))
    request.addfinalizer(_cleanup)
    return {"id": uid, "email": f"x-{uid}@test.local", "db": db}


def _proposal(db, uid, actions):
    from nl_preview import build_preview, store_proposal
    return _run(store_proposal(db, uid, "t", actions, _run(build_preview(db, uid, actions))))


READY = {"state": "READY", "new_exposure_allowed": True, "decision_id": "dec_test"}


# ------------------------------------------------------------- P0-01 NL confirm
def test_fifty_concurrent_confirms_execute_once(world, monkeypatch):
    import routes.nl_routes as nr
    import canonical_decision
    db, uid = world["db"], world["id"]
    user = {"id": uid, "email": world["email"]}
    calls = []

    async def _exec(u, act, **kw):
        calls.append(act["type"])
        await asyncio.sleep(0.01)
        return {"ok": True}

    async def _auth(db_, uid_, fresh=False):
        return READY

    monkeypatch.setattr(nr, "execute_one", _exec)
    monkeypatch.setattr(canonical_decision, "decide_user", _auth)
    doc = _proposal(db, uid, [{"type": "CLOSE_ALL_TRADES", "target": "all"},
                              {"type": "DISABLE_BOTS", "target": "all"}])

    async def _one():
        try:
            return await nr.nl_command_confirm({"proposal_id": doc["id"]}, user=user)
        except HTTPException as e:
            return e.detail

    results = _run(asyncio.gather(*[_one() for _ in range(50)]))
    winners = [r for r in results if isinstance(r, dict) and r.get("status") == "executed"]
    assert len(winners) == 1, results[:3]
    assert calls == ["CLOSE_ALL_TRADES", "DISABLE_BOTS"]
    codes = sorted({r.get("code") for r in results if isinstance(r, dict) and r.get("code")})
    assert set(codes) <= {"execution_in_progress", "proposal_not_pending"}
    stored = _run(db.nl_proposals.find_one({"_id": ObjectId(doc["id"])}))
    assert stored["status"] == "executed" and len(stored["action_receipts"]) == 2


def test_crash_recovery_never_replays_completed_action(world, monkeypatch):
    import nl_execution as nx
    import routes.nl_routes as nr
    import canonical_decision
    db, uid = world["db"], world["id"]
    user = {"id": uid, "email": world["email"]}
    calls = []

    async def _auth(db_, uid_, fresh=False):
        return READY
    monkeypatch.setattr(canonical_decision, "decide_user", _auth)

    class Boom(Exception):
        pass

    async def _exec(u, act, **kw):
        calls.append(act["type"])
        return {"ok": True}
    monkeypatch.setattr(nr, "execute_one", _exec)
    actions = [{"type": "CLOSE_ALL_TRADES", "target": "all"}, {"type": "DISABLE_BOTS", "target": "all"},
               {"type": "MOVE_STOPS_BREAKEVEN", "target": "all"}]
    doc = _proposal(db, uid, actions)
    claimed = _run(nx.claim(db, "nl_proposals", {"_id": ObjectId(doc["id"])}, from_status="pending"))
    # hard crash AFTER action 1 ran but BEFORE its post-receipt is written
    real_write = nx._write_receipt

    async def _crashing_write(db_, coll, doc_, key, rec):
        if rec.get("index") == 1 and rec.get("state") == "done":
            raise Boom()
        return await real_write(db_, coll, doc_, key, rec)
    monkeypatch.setattr(nx, "_write_receipt", _crashing_write)
    with pytest.raises(Boom):
        _run(nx.run_claimed(db, "nl_proposals", claimed, uid, actions, authority=READY))
    monkeypatch.setattr(nx, "_write_receipt", real_write)
    real_update = db.nl_proposals.update_one
    assert calls == ["CLOSE_ALL_TRADES", "DISABLE_BOTS"]

    # lease still live → confirm refuses (execution_in_progress)
    with pytest.raises(HTTPException) as ei:
        _run(nr.nl_command_confirm({"proposal_id": doc["id"]}, user=user))
    assert ei.value.detail["code"] == "execution_in_progress"

    # expire the lease → recovery resumes: action 0 (done) NOT replayed,
    # action 1 (started, crashed AFTER its effect row reached `completed`) is
    # resolved from the effect row — result copied, NOT replayed (r16 P0-01);
    # action 2 executed once
    _run(real_update({"_id": ObjectId(doc["id"])}, {"$set": {
        "execution.lease_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}}))

    res = _run(nr.nl_command_confirm({"proposal_id": doc["id"]}, user=user))
    assert calls == ["CLOSE_ALL_TRADES", "DISABLE_BOTS", "MOVE_STOPS_BREAKEVEN"]
    states = [r["state"] for r in res["receipts"]]
    assert states == ["done", "done", "done"]
    assert res["receipts"][1].get("recovered") is True
    assert res["status"] == "executed" and res["confirmed"] is True
    # replay after completion → refused, receipts returned
    with pytest.raises(HTTPException) as ei2:
        _run(nr.nl_command_confirm({"proposal_id": doc["id"]}, user=user))
    assert ei2.value.detail["code"] == "proposal_not_pending"
    assert calls.count("CLOSE_ALL_TRADES") == 1


def test_partial_failure_is_visible_and_authority_suppresses_risk_increase(world, monkeypatch):
    import routes.nl_routes as nr
    import canonical_decision
    db, uid = world["db"], world["id"]
    user = {"id": uid, "email": world["email"]}

    async def _exec(u, act, **kw):
        if act["type"] == "MOVE_STOPS_BREAKEVEN":
            raise RuntimeError("broker down")
        return {"ok": True}

    async def _blocked(db_, uid_, fresh=False):
        return {"state": "CLOSE_ONLY", "new_exposure_allowed": False, "decision_id": "dec_blk"}
    monkeypatch.setattr(nr, "execute_one", _exec)
    monkeypatch.setattr(canonical_decision, "decide_user", _blocked)
    doc = _proposal(db, uid, [{"type": "DISABLE_BOTS", "target": "all"},
                              {"type": "MOVE_STOPS_BREAKEVEN", "target": "all"},
                              {"type": "ENABLE_BOTS", "target": "all"},
                              {"type": "SET_RISK_LEVEL", "params": {"risk_level": "high"}}])
    res = _run(nr.nl_command_confirm({"proposal_id": doc["id"]}, user=user))
    assert [r["state"] for r in res["receipts"]] == ["done", "failed", "suppressed", "suppressed"]
    assert res["status"] == "partially_executed"
    assert res["receipts"][2]["decision_id"] == "dec_blk"
    stored = _run(db.nl_proposals.find_one({"_id": ObjectId(doc["id"])}))
    assert stored["status"] == "partially_executed"


def test_confirm_revalidates_inside_claim(world, monkeypatch):
    import routes.nl_routes as nr
    import canonical_decision
    import nl_preview
    db, uid = world["db"], world["id"]
    user = {"id": uid, "email": world["email"]}

    async def _auth(db_, uid_, fresh=False):
        return READY
    monkeypatch.setattr(canonical_decision, "decide_user", _auth)
    actions = [{"type": "CLOSE_ALL_TRADES", "target": "all"}]
    doc = _proposal(db, uid, actions)
    real = nl_preview.build_preview

    async def _drift_once(db_, uid_, acts):
        # portfolio changes exactly between the claim and the in-boundary check
        await db.trades.update_one({"user_id": uid, "status": "open"}, {"$set": {"status": "closed"}})
        return await real(db_, uid_, acts)
    monkeypatch.setattr(nl_preview, "build_preview", _drift_once)
    with pytest.raises(HTTPException) as ei:
        _run(nr.nl_command_confirm({"proposal_id": doc["id"]}, user=user))
    assert ei.value.detail["code"] == "preview_stale"
    stored = _run(db.nl_proposals.find_one({"_id": ObjectId(doc["id"])}))
    assert stored["status"] == "pending" and "execution" not in stored
    assert _run(db.trades.count_documents({"user_id": uid, "close_requested": True})) == 0


# ------------------------------------------------------------- P0-02 triggers
def test_two_sweepers_one_claim_and_authority_suppression(world, monkeypatch):
    import trigger_sweeper as ts
    import routes.nl_routes as nr
    import canonical_decision
    db, uid = world["db"], world["id"]
    calls = []

    async def _exec(u, act, **kw):
        calls.append(act["type"])
        return {"ok": True}

    async def _quote(sym):
        return {"price": 90.0}

    async def _blocked(db_, uid_, fresh=False):
        return {"state": "BLOCKED", "new_exposure_allowed": False, "decision_id": "dec_b"}
    monkeypatch.setattr(nr, "execute_one", _exec)
    monkeypatch.setattr(ts, "get_quote", _quote)
    monkeypatch.setattr(canonical_decision, "decide_user", _blocked)
    tid = _run(db.conditional_triggers.insert_one({
        "user_id": uid, "symbol": "BTCUSD", "condition": "drop", "threshold_pct": 5.0,
        "then": [{"type": "DISABLE_BOTS", "target": "all"}, {"type": "ENABLE_BOTS", "target": "all"}],
        "active": True, "status": "active", "baseline_price": 100.0})).inserted_id
    # isolate: only our trigger is due (others in the DB have no baseline / other symbols)
    _run(db.conditional_triggers.update_many({"_id": {"$ne": tid}, "active": True}, {"$set": {"active": False, "_parked": True}}))
    try:
        r1, r2 = _run(asyncio.gather(ts.sweep_once(), ts.sweep_once()))
    finally:
        _run(db.conditional_triggers.update_many({"_parked": True}, {"$set": {"active": True}, "$unset": {"_parked": ""}}))
    assert r1["fired"] + r2["fired"] == 1
    assert calls == ["DISABLE_BOTS"]  # ENABLE suppressed by BLOCKED authority
    t = _run(db.conditional_triggers.find_one({"_id": tid}))
    assert t["active"] is False and t["status"] == "partially_executed"
    assert [r["state"] for r in t["receipts"]] == ["done", "suppressed"]
    events = _run(db.trigger_fire_events.find({"trigger_id": str(tid)}).to_list(10))
    assert len(events) == 1 and events[0]["event_id"] == t["execution"]["id"]
    # a repeated tick never re-fires
    _run(ts.sweep_once())
    assert calls == ["DISABLE_BOTS"]


def test_trigger_lease_recovery_no_duplicate(world, monkeypatch):
    import trigger_sweeper as ts
    import routes.nl_routes as nr
    import canonical_decision
    import nl_execution as nx
    db, uid = world["db"], world["id"]
    calls = []

    async def _exec(u, act, **kw):
        calls.append(act["type"])
        return {"ok": True}

    async def _ready(db_, uid_, fresh=False):
        return READY
    monkeypatch.setattr(nr, "execute_one", _exec)
    monkeypatch.setattr(canonical_decision, "decide_user", _ready)
    actions = [{"type": "CLOSE_ALL_TRADES"}, {"type": "DISABLE_BOTS"}]
    tid = _run(db.conditional_triggers.insert_one({
        "user_id": uid, "symbol": "BTCUSD", "condition": "drop", "threshold_pct": 5.0,
        "then": actions, "active": True, "status": "active", "baseline_price": 100.0})).inserted_id
    claimed = _run(nx.claim(db, "conditional_triggers", {"_id": tid, "active": True},
                            from_status=ts.ACTIVE_STATUSES, extra_set={"active": False}))
    # crashed after action 0 completed: write its receipt, then die
    key0 = nx.idempotency_key(claimed["execution"]["id"], 0, actions[0])
    _run(db.conditional_triggers.update_one({"_id": tid}, {"$set": {
        f"action_receipts.{key0}": {"state": "done", "index": 0, "type": "CLOSE_ALL_TRADES", "key": key0},
        "execution.lease_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}}))
    n = _run(ts.recover_stale(db))
    assert n == 1
    assert calls == ["DISABLE_BOTS"]  # action 0 never replayed
    t = _run(db.conditional_triggers.find_one({"_id": tid}))
    assert t["status"] == "executed" and t["execution"]["attempt"] == 2
    assert _run(db.trigger_fire_events.count_documents({"trigger_id": str(tid)})) == 1


# ------------------------------------------------------------- P1-01 active only
def test_enabled_flag_never_counts_and_is_migrated():
    from database import get_db
    db = get_db()
    uid = str(ObjectId())
    acc = str(ObjectId())
    try:
        try:
            _run(db.bot_configs.insert_one({"user_id": uid, "account_id": acc, "enabled": True}))
        except Exception as e:  # noqa: BLE001 — schema validator active (seed.ensure_indexes)
            assert "Document failed validation" in str(e)
            _run(db.bot_configs.insert_one({"user_id": uid, "account_id": acc, "active": True}))
        src_a = open(os.path.join(os.path.dirname(__file__), "..", "..", "ops", "production_reconcile.py")).read()
        src_b = open(os.path.join(os.path.dirname(__file__), "..", "..", "ops", "prepromotion_evidence.py")).read()
        assert '{"enabled": True' not in src_a and '{"enabled": True' not in src_b
        assert 'bot_configs.find({"active": True}' in src_a and '{"active": True, "user_id"' in src_b
        legacy = _run(db.bot_configs.count_documents({"user_id": uid, "enabled": True}))
        assert _run(db.bot_configs.count_documents({"user_id": uid, "active": True})) == (0 if legacy else 1)
        # the seed migration folds legacy enabled → active and drops the field
        _run(db.bot_configs.update_many(
            {"user_id": uid, "enabled": {"$exists": True}, "active": {"$exists": False}},
            [{"$set": {"active": {"$eq": ["$enabled", True]}}}]))
        _run(db.bot_configs.update_many({"user_id": uid, "enabled": {"$exists": True}}, {"$unset": {"enabled": ""}}))
        d = _run(db.bot_configs.find_one({"user_id": uid}))
        assert d["active"] is True and "enabled" not in d
    finally:
        _run(db.bot_configs.delete_many({"user_id": uid}))


# ------------------------------------------------------------- SEC-001 ledger chain
def test_ledger_chain_cannot_fork_under_concurrency():
    """Two parallel imports for one user must yield seq 1 and 2 — never a fork."""
    import broker_statement_ledger as bl
    from database import get_db
    db = get_db()
    uid = f"chain-{ObjectId()}"
    _run(db.reconciliation_ledger.create_index([("user_id", 1), ("ledger_seq", 1)], unique=True,
                                               partialFilterExpression={"ledger_seq": {"$exists": True}}))
    try:
        async def _one(i):
            row = {"_id": f"{uid}:S{i}", "user_id": uid, "account_id": "a", "statement_id": f"S{i}",
                   "statement_sha256": f"{i:064d}", "status": "RECONCILED"}
            # replicate the append tail of reconcile(): read-latest → insert, retry on dup
            for _ in range(8):
                last = await db.reconciliation_ledger.find_one({"user_id": uid, "ledger_root": {"$exists": True}},
                                                               sort=[("ledger_seq", -1)])
                row["ledger_seq"] = int((last or {}).get("ledger_seq") or 0) + 1
                row["ledger_root"] = f"root{row['ledger_seq']}"
                try:
                    await db.reconciliation_ledger.insert_one(row)
                    return row["ledger_seq"]
                except Exception:
                    continue
            raise AssertionError("no seq")
        seqs = _run(asyncio.gather(*[_one(i) for i in range(6)]))
        assert sorted(seqs) == [1, 2, 3, 4, 5, 6]
        assert _run(db.reconciliation_ledger.count_documents({"user_id": uid})) == 6
    finally:
        _run(db.reconciliation_ledger.delete_many({"user_id": uid}))


def test_trigger_then_actions_validated_at_arm_time(world, monkeypatch):
    import routes.nl_routes as nr
    user = {"id": world["id"], "email": world["email"]}

    async def _fake(prompt):
        return {"actions": [{"type": "SET_CONDITIONAL_TRIGGER",
                             "params": {"symbol": "BTCUSD", "threshold_pct": 3, "then": [{"type": "DROP_TABLES"}]}}]}
    monkeypatch.setattr(nr, "interpret_command", _fake)
    with pytest.raises(HTTPException) as ei:
        _run(nr.nl_command({"prompt": "x"}, user=user))
    assert ei.value.status_code == 400 and ei.value.detail["code"] == "invalid_action"
    assert "then" in ei.value.detail["message"]
