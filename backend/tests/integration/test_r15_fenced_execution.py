"""Audit r15 — fenced exactly-once execution (P0-01), typed risk semantics
(P1-01). REQUIRED CI lane (critical_controls)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

pytestmark = [pytest.mark.integration, pytest.mark.critical_controls]

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), ".env"))

READY = {"state": "READY", "new_exposure_allowed": True, "decision_id": "dec_test"}


def _run(coro):
    from conftest import run_async
    return run_async(coro)


@pytest.fixture
def world(request):
    from database import get_db
    db = get_db()
    uid = str(ObjectId())

    def _cleanup():
        for c in ("nl_proposals", "conditional_triggers", "trigger_fire_events", "trades", "bot_configs"):
            _run(db[c].delete_many({"user_id": uid}))
        _run(db.nl_effects.delete_many({"user_id": uid}))
    request.addfinalizer(_cleanup)
    return {"id": uid, "db": db}


def _expire(db, coll, _id):
    _run(db[coll].update_one({"_id": _id}, {"$set": {
        "execution.lease_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}}))


@pytest.mark.parametrize("coll", ["nl_proposals", "conditional_triggers"])
def test_stale_worker_resuming_after_reclaim_executes_nothing(world, coll):
    """r16 P0-01: A stalls BEFORE its side effect past the lease; B reclaims, takes
    over the effect row (never applied) and runs everything; A resumes and hits
    apply_effect → fenced out → executes NOTHING. Every effect row ends
    `completed` under fence 2."""
    import nl_execution as nx
    db, uid = world["db"], world["id"]
    actions = [{"type": "CLOSE_ALL_TRADES", "target": "all"}, {"type": "DISABLE_BOTS", "target": "all"}]
    _id = _run(db[coll].insert_one({"user_id": uid, "status": "pending", "actions": actions})).inserted_id
    a_doc = _run(nx.claim(db, coll, {"_id": _id}, from_status="pending"))
    gate = asyncio.Event()
    stalled = asyncio.Event()
    calls = []

    async def _exec_a(u, act, ctx=None, **kw):
        if act["type"] == "CLOSE_ALL_TRADES":
            stalled.set()
            await gate.wait()          # A stalls right before its domain write
        await nx.apply_effect(db, ctx)  # the ultimate handler's boundary
        calls.append(("A", act["type"]))
        return {"ok": True}

    async def _exec_b(u, act, ctx=None, **kw):
        await nx.apply_effect(db, ctx)
        calls.append(("B", act["type"]))
        return {"ok": True}

    async def _scenario():
        task_a = asyncio.create_task(nx.run_claimed(db, coll, a_doc, uid, actions, authority=READY, execute_one=_exec_a))
        await asyncio.wait_for(stalled.wait(), timeout=10)
        await db[coll].update_one({"_id": _id}, {"$set": {
            "execution.lease_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}})
        b_doc = await nx.reclaim_expired(db, coll, await db[coll].find_one({"_id": _id}))
        assert b_doc and b_doc["execution"]["fence"] == 2 and b_doc["execution"]["owner"] != a_doc["execution"]["owner"]
        res_b = await nx.run_claimed(db, coll, b_doc, uid, actions, authority=READY, execute_one=_exec_b)
        gate.set()                      # A wakes up late
        res_a = await task_a
        return res_a, res_b

    res_a, res_b = _run(_scenario())
    assert res_a["status"] == "lease_lost"
    assert calls == [("B", "CLOSE_ALL_TRADES"), ("B", "DISABLE_BOTS")]   # A executed NOTHING
    assert [r["state"] for r in res_b["receipts"]] == ["done", "done"] and res_b["status"] == "executed"
    final = _run(db[coll].find_one({"_id": _id}))
    assert final["status"] == "executed" and final["execution"]["fence"] == 2
    assert all(r["fence"] == 2 for r in final["action_receipts"].values())
    rows = _run(db.nl_effects.find({"user_id": uid}).to_list(10))
    assert len(rows) == 2 and all(r["state"] == "completed" and r["fence"] == 2 for r in rows)
    assert rows[0]["taken_over_from"] == a_doc["execution"]["owner"] or rows[1].get("taken_over_from")


def test_stale_worker_mid_write_is_recorded_uncertain_and_never_replayed(world):
    """A reached `applying` (may be inside the write) and stalls; B must NOT
    re-run the action: receipt uncertain, effect row fenced so A's completion fails."""
    import nl_execution as nx
    db, uid = world["db"], world["id"]
    actions = [{"type": "CLOSE_ALL_TRADES", "target": "all"}]
    _id = _run(db.nl_proposals.insert_one({"user_id": uid, "status": "pending", "actions": actions})).inserted_id
    a_doc = _run(nx.claim(db, "nl_proposals", {"_id": _id}, from_status="pending"))
    gate = asyncio.Event()
    applying = asyncio.Event()
    calls = []

    async def _exec_a(u, act, ctx=None, **kw):
        await nx.apply_effect(db, ctx)
        applying.set()                  # A is now provably inside the domain write
        await gate.wait()               # stalled INSIDE the domain write
        calls.append("A")
        return {"ok": True}

    async def _exec_b(u, act, ctx=None, **kw):
        calls.append("B")
        return {"ok": True}

    async def _scenario():
        task_a = asyncio.create_task(nx.run_claimed(db, "nl_proposals", a_doc, uid, actions, authority=READY, execute_one=_exec_a))
        await asyncio.wait_for(applying.wait(), timeout=10)
        await db.nl_proposals.update_one({"_id": _id}, {"$set": {
            "execution.lease_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}})
        b_doc = await nx.reclaim_expired(db, "nl_proposals", await db.nl_proposals.find_one({"_id": _id}))
        res_b = await nx.run_claimed(db, "nl_proposals", b_doc, uid, actions, authority=READY, execute_one=_exec_b)
        gate.set()
        res_a = await task_a
        return res_a, res_b

    res_a, res_b = _run(_scenario())
    assert calls == ["A"] and res_a["status"] == "lease_lost"       # B never replayed it
    assert res_b["receipts"][0]["state"] == "uncertain" and res_b["status"] == "failed"
    row = _run(db.nl_effects.find_one({"user_id": uid}))
    assert row["state"] == "uncertain" and row["fence"] == 2          # A's completion was fenced out


def test_authority_is_rechecked_immediately_before_each_effect(world, monkeypatch):
    """Authority moves between confirmation and the effect: risk-increasing
    ENABLE_BOTS is refused (effect row failed, receipt suppressed with the fresh
    decision id); risk-reducing DISABLE_BOTS still runs."""
    import nl_execution as nx
    import canonical_decision as cd
    db, uid = world["db"], world["id"]
    actions = [{"type": "DISABLE_BOTS", "target": "all"}, {"type": "ENABLE_BOTS", "target": "all"}]
    _id = _run(db.nl_proposals.insert_one({"user_id": uid, "status": "pending", "actions": actions})).inserted_id
    doc = _run(nx.claim(db, "nl_proposals", {"_id": _id}, from_status="pending"))
    confirmed = {**READY, "input_version": 7}

    async def _fresh(db_, user_id, fresh=False):
        return {"state": "READY", "new_exposure_allowed": True, "decision_id": "dec_fresh", "input_version": 8}
    monkeypatch.setattr(cd, "decide_user", _fresh)
    calls = []

    async def _exec(u, act, ctx=None, **kw):
        await nx.apply_effect(db, ctx)
        calls.append((act["type"], ctx["authority_version"], ctx["decision_id"]))
        return {"ok": True}

    res = _run(nx.run_claimed(db, "nl_proposals", doc, uid, actions, authority=confirmed, execute_one=_exec))
    assert calls == [("DISABLE_BOTS", 8, "dec_fresh")]
    assert [r["state"] for r in res["receipts"]] == ["done", "suppressed"]
    assert "changed since confirmation" in res["receipts"][1]["reason"] and res["receipts"][1]["decision_id"] == "dec_fresh"
    rows = {r["type"]: r for r in _run(db.nl_effects.find({"user_id": uid}).to_list(10))}
    assert rows["DISABLE_BOTS"]["state"] == "completed" and rows["ENABLE_BOTS"]["state"] == "failed"


def test_real_handlers_stamp_rows_and_refuse_stale_fence(world):
    """The ultimate handlers call apply_effect and stamp mutated rows; a ctx
    whose fence was superseded writes nothing."""
    import nl_execution as nx
    from routes.nl_routes import execute_one
    db, uid = world["db"], world["id"]
    _run(db.trades.insert_many([{"user_id": uid, "symbol": "XAUUSD", "status": "open", "action": "BUY", "entry_price": 1.0},
                                {"user_id": uid, "symbol": "BTCUSD", "status": "open", "action": "BUY", "entry_price": 1.0}]))
    key = "f" * 32
    ctx = {"idempotency_key": key, "execution_id": "ex1", "owner": "w1", "fence": 1, "decision_id": "d1", "authority_version": 3}
    assert _run(nx.reserve_effect(db, key, {"user_id": uid, "type": "CLOSE_ALL_TRADES"}, ctx=ctx)) == "reserved"
    assert _run(nx.dispatch_effect(db, ctx)) is True
    # a recovery worker with fence 2 takes the row over before w1 writes
    ctx2 = {**ctx, "owner": "w2", "fence": 2}
    assert _run(nx.reserve_effect(db, key, {"user_id": uid}, ctx=ctx2)) == "reserved"
    with pytest.raises(nx.LeaseLost):
        _run(execute_one(uid, {"type": "CLOSE_ALL_TRADES", "target": "XAUUSD"}, idem_key=key, ctx=ctx))
    assert _run(db.trades.count_documents({"user_id": uid, "close_requested": True})) == 0
    assert _run(nx.dispatch_effect(db, ctx2)) is True
    out = _run(execute_one(uid, {"type": "CLOSE_ALL_TRADES", "target": "XAUUSD"}, idem_key=key, ctx=ctx2))
    assert out["trades_marked_for_close"] == 1
    t = _run(db.trades.find_one({"user_id": uid, "symbol": "XAUUSD"}))
    assert t["close_idem_key"] == key and t["close_fence"] == 2 and t["nl_effect"]["decision_id"] == "d1"
    assert _run(db.trades.find_one({"user_id": uid, "symbol": "BTCUSD"})).get("close_requested") is None


def test_bot_targets_are_action_specific(world):
    """r16 P1-01: a symbol token must NEVER widen a bot action to every bot."""
    from nl_actions import validate_actions
    from nl_preview import bot_query, build_preview
    from routes.nl_routes import _disable_bots
    db, uid = world["db"], world["id"]
    ids = _run(db.bot_configs.insert_many([{"user_id": uid, "account_id": str(ObjectId()), "active": True, "risk_level": "low"},
                                           {"user_id": uid, "account_id": str(ObjectId()), "active": True, "risk_level": "extreme"}])).inserted_ids
    for bad in ([{"type": "DISABLE_BOTS", "target": "XAUUSD"}], [{"type": "ENABLE_BOTS", "target": "bot:zzz"}],
                [{"type": "SET_RISK_LEVEL", "target": "BTCUSD", "params": {"risk_level": "low"}}],
                [{"type": "CLOSE_ALL_TRADES", "target": f"bot:{ids[0]}"}], [{"type": "PANIC_LOCK", "target": "XAUUSD"}]):
        with pytest.raises(ValueError):
            validate_actions(bad)
    ok = validate_actions([{"type": "disable_bots", "target": f"BOT:{ids[1]}"}, {"type": "close_all_trades", "target": "xauusd"},
                           {"type": "panic_lock"}, {"type": "set_risk_level", "target": "high_risk", "params": {"risk_level": "LOW"}}])
    assert ok[0]["target"] == f"bot:{ids[1]}" and ok[1]["target"] == "XAUUSD" and ok[2]["target"] == "all"
    with pytest.raises(ValueError):
        bot_query(uid, "XAUUSD")
    with pytest.raises(ValueError):
        _run(_disable_bots(uid, "XAUUSD"))
    assert _run(db.bot_configs.count_documents({"user_id": uid, "active": True})) == 2
    assert _run(_disable_bots(uid, f"bot:{ids[1]}"))["bots_disabled"] == 1
    assert _run(db.bot_configs.find_one({"_id": ids[0]}))["active"] is True
    pv = _run(build_preview(db, uid, ok))
    assert pv["actions"][0]["resolved_ids"] == []                       # bot 1 is already disabled → nothing to touch
    assert pv["actions"][3]["resolved_ids"] == [f"{ids[1]}:extreme"]    # high_risk scope resolves to the immutable id
    assert all(len(a["inventory_hash"]) == 16 for a in pv["actions"])


def test_effect_boundary_rejects_duplicate_key(world):
    """The same idempotency key can produce ONE side effect, ever — even if a
    stale worker slipped past the document guard."""
    import nl_execution as nx
    db, uid = world["db"], world["id"]
    key = "k" * 32
    ctx = {"idempotency_key": key, "execution_id": "e", "owner": "w1", "fence": 1}
    assert _run(nx.reserve_effect(db, key, {"user_id": uid}, ctx=ctx)) == "reserved"
    assert _run(nx.reserve_effect(db, key, {"user_id": uid}, ctx=ctx)) == "lost"          # same fence: no takeover
    assert _run(nx.dispatch_effect(db, ctx)) and _run(nx.complete_effect(db, ctx, "completed", {"n": 1}))
    assert _run(nx.reserve_effect(db, key, {"user_id": uid}, ctx={**ctx, "owner": "w2", "fence": 2})) == "completed"
    with pytest.raises(nx.LeaseLost):
        _run(nx.apply_effect(db, ctx))


def test_three_workers_one_fence_per_action(world):
    """Stale original + two recovery workers racing: every action starts under
    exactly one fence and no action runs twice."""
    import nl_execution as nx
    db, uid = world["db"], world["id"]
    actions = [{"type": "DISABLE_BOTS", "target": "all"}, {"type": "MOVE_STOPS_BREAKEVEN", "target": "all"}]
    _id = _run(db.nl_proposals.insert_one({"user_id": uid, "status": "pending", "actions": actions})).inserted_id
    stale = _run(nx.claim(db, "nl_proposals", {"_id": _id}, from_status="pending"))
    _expire(db, "nl_proposals", _id)
    calls = []

    async def _exec(u, act, **kw):
        calls.append(act["type"])
        await asyncio.sleep(0.01)
        return {"ok": True}

    async def _recover():
        doc = await db.nl_proposals.find_one({"_id": _id})
        got = await nx.reclaim_expired(db, "nl_proposals", doc)
        if got is None:
            return "lost_reclaim"
        return (await nx.run_claimed(db, "nl_proposals", got, uid, actions, authority=READY, execute_one=_exec))["status"]

    results = _run(asyncio.gather(
        nx.run_claimed(db, "nl_proposals", stale, uid, actions, authority=READY, execute_one=_exec),
        _recover(), _recover()))
    assert results[0]["status"] == "lease_lost"
    assert sorted(results[1:]) == ["executed", "lost_reclaim"]
    assert calls == ["DISABLE_BOTS", "MOVE_STOPS_BREAKEVEN"]
    final = _run(db.nl_proposals.find_one({"_id": _id}))
    assert final["status"] == "executed" and final["execution"]["fence"] == 2


def test_breakeven_is_monotonic_risk_reduction(world):
    from routes.nl_routes import _move_stops_breakeven, breakeven_decision
    db, uid = world["db"], world["id"]
    _run(db.trades.insert_many([
        {"user_id": uid, "symbol": "XAUUSD", "action": "BUY", "status": "open", "entry_price": 4000.0, "stop_loss": 4010.0, "current_price": 4050.0, "tag": "buy_better"},
        {"user_id": uid, "symbol": "XAUUSD", "action": "SELL", "status": "open", "entry_price": 4000.0, "stop_loss": 3990.0, "current_price": 3950.0, "tag": "sell_better"},
        {"user_id": uid, "symbol": "XAUUSD", "action": "BUY", "status": "open", "entry_price": 4000.0, "stop_loss": 3980.0, "current_price": 4050.0, "tag": "buy_worse"},
        {"user_id": uid, "symbol": "XAUUSD", "action": "SELL", "status": "open", "entry_price": 4000.0, "stop_loss": 4020.0, "current_price": 3950.0, "tag": "sell_worse"},
        {"user_id": uid, "symbol": "XAUUSD", "action": "BUY", "status": "open", "entry_price": 4000.0, "stop_loss": 3980.0, "current_price": 3990.0, "tag": "buy_underwater"},
    ]))
    res = _run(_move_stops_breakeven(uid, "all"))
    assert res["trades_updated"] == 2
    got = {t["tag"]: t["stop_loss"] for t in _run(db.trades.find({"user_id": uid}).to_list(10))}
    assert got == {"buy_better": 4010.0, "sell_better": 3990.0, "buy_worse": 4000.0, "sell_worse": 4000.0,
                   "buy_underwater": 3980.0}
    assert breakeven_decision({"action": "BUY", "entry_price": 1.0, "stop_loss": 2.0})["move"] is False
    assert breakeven_decision({"action": "SELL", "entry_price": 1.0, "stop_loss": None, "current_price": 0.9})["move"] is True
    # idempotent second run: nothing moves
    assert _run(_move_stops_breakeven(uid, "all"))["trades_updated"] == 0


def test_executor_errors_are_failed_never_done(world):
    from routes.nl_routes import execute_one
    import nl_execution as nx
    db, uid = world["db"], world["id"]
    actions = [{"type": "SET_RISK_LEVEL", "params": {"risk_level": "yolo"}}, {"type": "NOPE"}]
    _id = _run(db.nl_proposals.insert_one({"user_id": uid, "status": "pending", "actions": actions})).inserted_id
    doc = _run(nx.claim(db, "nl_proposals", {"_id": _id}, from_status="pending"))
    res = _run(nx.run_claimed(db, "nl_proposals", doc, uid, actions, authority=READY, execute_one=execute_one))
    assert [r["state"] for r in res["receipts"]] == ["failed", "failed"] and res["status"] == "failed"


def test_typed_schema_rejects_bad_parameters():
    from nl_actions import validate_actions
    ok = validate_actions([{"type": "set_conditional_trigger", "params": {"symbol": "btcusd", "threshold_pct": 4,
                                                                          "then": [{"type": "disable_bots"}]}}])
    assert ok[0]["params"]["symbol"] == "BTCUSD" and ok[0]["params"]["then"][0]["type"] == "DISABLE_BOTS"
    bad = [
        [{"type": "SET_CONDITIONAL_TRIGGER", "params": {"threshold_pct": 0, "then": [{"type": "DISABLE_BOTS"}]}}],
        [{"type": "SET_CONDITIONAL_TRIGGER", "params": {"threshold_pct": -3, "then": [{"type": "DISABLE_BOTS"}]}}],
        [{"type": "SET_CONDITIONAL_TRIGGER", "params": {"threshold_pct": float("inf"), "then": [{"type": "DISABLE_BOTS"}]}}],
        [{"type": "SET_CONDITIONAL_TRIGGER", "params": {"threshold_pct": float("nan"), "then": [{"type": "DISABLE_BOTS"}]}}],
        [{"type": "SET_CONDITIONAL_TRIGGER", "params": {"threshold_pct": 99, "then": [{"type": "DISABLE_BOTS"}]}}],
        [{"type": "SET_CONDITIONAL_TRIGGER", "params": {"threshold_pct": 3, "condition": "crash", "then": [{"type": "DISABLE_BOTS"}]}}],
        [{"type": "SET_CONDITIONAL_TRIGGER", "params": {"threshold_pct": 3, "then": [{"type": "DISABLE_BOTS"}] * 4}}],
        [{"type": "SET_CONDITIONAL_TRIGGER", "params": {"threshold_pct": 3, "then": [{"type": "SET_CONDITIONAL_TRIGGER", "params": {"threshold_pct": 3, "then": [{"type": "DISABLE_BOTS"}]}}]}}],
        [{"type": "SET_RISK_LEVEL", "params": {"risk_level": "yolo"}}],
        [{"type": "DISABLE_BOTS", "target": "'; drop"}],
        [{"type": "DISABLE_BOTS"}] * 9,
        [{"type": "DELETE_EVERYTHING"}],
        [],
    ]
    for b in bad:
        with pytest.raises(ValueError):
            validate_actions(b)


# ------------------------------------------------------------- P1-02 public readiness
def test_public_status_is_availability_only_unless_exact_signed_inventory():
    import requests
    from live_target import get_base_url
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "routes", "portal_routes.py")).read()
    block = src[src.index("audit r15 P1-02"):src.index("trading_ready = ")]
    for needle in ('proj["approved_hash"] == proj["inventory_hash"]', "not proj[\"violations\"]",
                   "not proj[\"structural_defects\"]", "v_before == v_after", 'st == "READY" for st in states',
                   "exp_declared"):
        assert needle in block, needle
    payload = src[src.index("trading = {"):src.index("headline = (")]
    for forbidden in ("fresh_terminals", "enabled_accounts_present", "accounts_enabled", "accounts_ready", "sig"):
        assert forbidden not in payload, forbidden
    base = get_base_url()
    if base:
        d = requests.get(f"{base}/api/status", timeout=20).json()
        assert d["headline"].startswith("Platform controls available") or d["headline"].startswith("Platform ")
        assert set(d["trading"]["attestation"]) == {"attested", "basis", "input_version", "inventory_hash", "as_of"}
        if not d["trading"]["attestation"]["attested"]:
            assert d["trading"]["label"] != "Trading ready"
            assert d["trading"]["readiness"]["new_exposure_allowed"] is False


# ------------------------------------------------------------- P1-03 ledger revalidation
def test_ledger_chain_tamper_and_identity_change_withhold(world):
    import hashlib
    import broker_statement_ledger as bl
    db, uid = world["db"], world["id"]
    acc = ObjectId()
    try:
        _run(db.accounts.insert_one({"_id": acc, "user_id": uid, "trading_enabled": True, "base_currency": "USD",
                                     "bridge_token": f"t-{acc}", "verified_identity": {"account_number": "5001"}}))
        rows = []
        prev = ""
        for i in (1, 2):
            sha = hashlib.sha256(f"stmt{i}".encode()).hexdigest()
            root = hashlib.sha256(f"{prev}|{sha}|RECONCILED".encode()).hexdigest()
            rows.append({"_id": f"{acc}:S{i}", "user_id": uid, "account_id": str(acc), "statement_id": f"S{i}",
                         "status": "RECONCILED", "currency": "USD", "broker_login": "5001", "statement_sha256": sha,
                         "ledger_seq": i, "ledger_root": root, "ledger_root_sig": bl._root_sig(uid, i, root),
                         "period_from": (datetime.now(timezone.utc) - timedelta(days=40 - 20 * (i - 1))).isoformat(),
                         "period_to": (datetime.now(timezone.utc) - timedelta(days=20 - 20 * (i - 1) + 1)).isoformat()})
            prev = root
        _run(db.reconciliation_ledger.insert_many(rows))
        assert _run(bl.verify_chain(db, uid))["problems"] == []
        reasons = _run(bl.ledger_gate(db, uid))
        assert "LEDGER_CHAIN_BROKEN" not in reasons and "STATEMENT_IDENTITY_CHANGED" not in reasons
        assert "STATEMENT_UNVERIFIABLE" in reasons          # no statement_raw → cannot re-verify signature
        # database tamper: flip one status → root mismatch → chain broken
        _run(db.reconciliation_ledger.update_one({"_id": f"{acc}:S1"}, {"$set": {"status": "DISCREPANCY"}}))
        assert "LEDGER_CHAIN_BROKEN" in _run(bl.ledger_gate(db, uid))
        _run(db.reconciliation_ledger.update_one({"_id": f"{acc}:S1"}, {"$set": {"status": "RECONCILED"}}))
        # account login changed after the statement was accepted
        _run(db.accounts.update_one({"_id": acc}, {"$set": {"verified_identity": {"account_number": "9999"}}}))
        assert "STATEMENT_IDENTITY_CHANGED" in _run(bl.ledger_gate(db, uid))
        # currency mismatch
        _run(db.accounts.update_one({"_id": acc}, {"$set": {"verified_identity": {"account_number": "5001"}, "base_currency": "EUR"}}))
        assert "STATEMENT_CURRENCY_MISMATCH" in _run(bl.ledger_gate(db, uid))
    finally:
        _run(db.accounts.delete_one({"_id": acc}))
        _run(db.reconciliation_ledger.delete_many({"user_id": uid}))


def test_platform_totals_never_sum_across_currencies(world):
    import broker_statement_ledger as bl
    db, uid = world["db"], world["id"]
    acc = str(ObjectId())
    now = datetime.now(timezone.utc)
    try:
        _run(db.broker_deals.insert_many([
            {"user_id": uid, "account_id": acc, "deal_id": 1, "deal_time": int((now - timedelta(days=2)).timestamp()), "profit": 10, "financial_reconciliation_status": "complete"},
            {"user_id": uid, "account_id": acc, "deal_id": 2, "deal_time": int((now - timedelta(days=2)).timestamp()), "profit": 99, "profit_currency": "EUR", "financial_reconciliation_status": "complete"},
        ]))
        tot = _run(bl.platform_totals(db, uid, acc, (now - timedelta(days=5)).isoformat(), now.isoformat(), currency="USD"))
        assert tot["trading_pnl"] == 10.0 and tot["foreign_currency_rows"] == 1 and tot["deals"] == 1
    finally:
        _run(db.broker_deals.delete_many({"user_id": uid}))


# ------------------------------------------------------------- P1-04 model governance recovery
def _governance_world(db, uid):
    import hashlib
    import io
    import joblib
    import model_manifest as mm
    import model_store as ms
    buf = io.BytesIO()
    joblib.dump({"uid": uid}, buf)
    cand = buf.getvalue()
    cd = hashlib.sha256(cand).hexdigest()
    _run(ms.put(db, cand, cd, uid, {"code_commit": "deadbeef", "manifest_sha256": "0" * 64, "created_at": "now",
                                    "artifact": {"sha256": cd, "bytes": len(cand)}, "builder": "test"}))
    _run(db.ml_ensembles.insert_one({"user_id": uid, "production": None,
                                     "candidate": {"status": "awaiting_approval", "digest": cd, "weights": {"xgboost": 1.0},
                                                   "aucs": {"xgboost": 0.7}, "n_trades": 140, "approvals": []}}))
    return cd


def test_approval_retryable_after_injected_failure_and_tenant_scoped(world, monkeypatch):
    import ml_ensemble as me
    import audit_chain
    db, uid = world["db"], world["id"]
    other = str(ObjectId())
    p1 = str(ObjectId())
    cd = _governance_world(db, uid)
    try:
        _governance_world(db, other)  # same principal, other tenant → must not collide
        real_append = audit_chain.append_chained

        async def _boom(*a, **k):
            raise RuntimeError("audit store down")
        monkeypatch.setattr(audit_chain, "append_chained", _boom)
        with pytest.raises(RuntimeError):
            _run(me.approve_candidate(db, uid, f"a-{uid}@x", "n", actor_id=p1))
        assert _run(db.model_approval_principals.count_documents({"user_id": uid, "digest": cd})) == 0  # reservation released
        monkeypatch.setattr(audit_chain, "append_chained", real_append)
        rec = _run(me.approve_candidate(db, uid, f"a-{uid}@x", "n", actor_id=p1))["approval"]
        assert rec["principal_id"] == p1
        assert _run(db.model_approval_principals.find_one({"user_id": uid, "digest": cd}))["status"] == "committed"
        assert _run(db.admin_audit_log.count_documents({"meta.digest": cd, "meta.user_id": uid, "action": "model_candidate_approved"})) == 1
        with pytest.raises(Exception) as ei:
            _run(me.approve_candidate(db, uid, f"a2-{uid}@x", "again", actor_id=p1))
        assert ei.value.status_code == 409
        # same principal / same digest for ANOTHER tenant is allowed
        rec2 = _run(me.approve_candidate(db, other, f"a-{other}@x", "n", actor_id=p1))["approval"]
        assert rec2["principal_id"] == p1
    finally:
        for u in (uid, other):
            _run(db.ml_ensembles.delete_many({"user_id": u}))
            _run(db.model_approval_principals.delete_many({"user_id": u}))
            _run(db.admin_audit_log.delete_many({"meta.user_id": u}))
            _run(db.model_artifact_meta.delete_many({"user_id": u}))


def test_outbox_cannot_clear_until_chained_event_exists(world, monkeypatch):
    import ml_ensemble as me
    import audit_chain
    db, uid = world["db"], world["id"]
    pid = f"promo-{uid}"
    ob = {"promotion_id": pid, "actor": "ops@x", "at": datetime.now(timezone.utc).isoformat(), "digest": "d" * 64,
          "manifest_sha256": "m" * 64, "approvals": [], "code_commit": "abc"}
    _run(db.ml_ensembles.insert_one({"user_id": uid, "production": {"status": "active", "digest": "d" * 64},
                                     "candidate": None, "promotion_outbox": ob}))
    try:
        real_append = audit_chain.append_chained

        async def _boom(*a, **k):
            raise RuntimeError("chain down")
        monkeypatch.setattr(audit_chain, "append_chained", _boom)
        with pytest.raises(RuntimeError):
            _run(me.flush_promotion_outbox(db, uid))
        assert _run(db.ml_ensembles.find_one({"user_id": uid}))["promotion_outbox"] is not None   # NOT cleared
        pub = _run(db.promotion_publications.find_one({"promotion_id": pid}))
        assert pub["status"] == "claimed"
        # a second flusher while the claim lease is live must not clear it either
        monkeypatch.setattr(audit_chain, "append_chained", real_append)
        assert _run(me.flush_promotion_outbox(db, uid)) == 0
        assert _run(db.ml_ensembles.find_one({"user_id": uid}))["promotion_outbox"] is not None
        # lease expiry → reclaim → the chained event is appended exactly once → cleared
        _run(db.promotion_publications.update_one({"promotion_id": pid}, {"$set": {
            "lease_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}}))
        assert _run(me.flush_promotion_outbox(db, uid)) == 1
        assert _run(db.ml_ensembles.find_one({"user_id": uid}))["promotion_outbox"] is None
        assert _run(db.admin_audit_log.count_documents({"action": "model_promoted", "meta.promotion_id": pid})) == 1
        assert _run(db.promotion_publications.find_one({"promotion_id": pid}))["status"] == "published"
    finally:
        _run(db.ml_ensembles.delete_many({"user_id": uid}))
        _run(db.promotion_publications.delete_many({"promotion_id": pid}))
        _run(db.admin_audit_log.delete_many({"meta.promotion_id": pid}))
