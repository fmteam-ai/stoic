"""Audit r17 — P0-01 destination-enforced fence (transaction / fail-closed),
P1-01 immutable approved targets (executed + reused on recovery), P0-02 read-only
live suite, P2-01 paired admission manifest, P2-02 provider idempotency."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

pytestmark = [pytest.mark.integration, pytest.mark.critical_controls]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, "backend", ".env"))

READY = {"state": "READY", "new_exposure_allowed": True, "decision_id": "dec_test", "input_version": 1}


def _run(coro):
    from conftest import run_async
    return run_async(coro)


@pytest.fixture
def world(request):
    from database import get_db
    db = get_db()
    uid = str(ObjectId())

    def _cleanup():
        for c in ("nl_proposals", "conditional_triggers", "trades", "bot_configs", "nl_effects", "nl_risk_level_audit"):
            _run(db[c].delete_many({"user_id": uid}))
    request.addfinalizer(_cleanup)
    return {"id": uid, "db": db}


def _bot(uid, risk="low", active=True):
    return {"user_id": uid, "account_id": str(ObjectId()), "active": active, "risk_level": risk}


def test_only_approved_ids_are_mutated_even_if_inventory_grows(world, monkeypatch):
    """P1-01: the preview bound 2 bots; a third bot appears before the effect →
    it is NOT touched. The receipt/effect row carry the exact target ids."""
    import nl_execution as nx
    import canonical_decision as cd
    from nl_preview import build_preview, store_proposal
    from routes.nl_routes import execute_one
    db, uid = world["db"], world["id"]
    ids = _run(db.bot_configs.insert_many([_bot(uid), _bot(uid)])).inserted_ids
    actions = [{"type": "DISABLE_BOTS", "target": "all"}]
    doc = _run(store_proposal(db, uid, "disable all", actions, _run(build_preview(db, uid, actions))))
    late = _run(db.bot_configs.insert_one(_bot(uid))).inserted_id          # appears AFTER approval

    async def _fresh(db_, user_id, fresh=False):
        return READY
    monkeypatch.setattr(cd, "decide_user", _fresh)
    claimed = _run(nx.claim(db, "nl_proposals", {"_id": doc["_id"]}, from_status="pending"))
    res = _run(nx.run_claimed(db, "nl_proposals", claimed, uid, actions, authority=READY, execute_one=execute_one))
    assert res["status"] == "executed" and res["receipts"][0]["result"] == {"bots_disabled": 2}
    assert sorted(res["receipts"][0]["target_ids"]) == sorted(str(i) for i in ids)
    assert _run(db.bot_configs.find_one({"_id": late}))["active"] is True
    row = _run(db.nl_effects.find_one({"user_id": uid}))
    assert sorted(row["target_ids"]) == sorted(str(i) for i in ids) and row["state"] == "completed"
    assert row["committed"] in ("transaction", "cas")


def test_recovery_reuses_original_bound_targets(world):
    """P1-01: a recovering worker must execute the ORIGINAL approved ids, never a
    re-resolved broad token — even when high_risk membership changed meanwhile."""
    import nl_execution as nx
    db, uid = world["db"], world["id"]
    b1 = _run(db.bot_configs.insert_one(_bot(uid, "extreme"))).inserted_id
    actions = [{"type": "DISABLE_BOTS", "target": "high_risk"}]
    _id = _run(db.nl_proposals.insert_one({"user_id": uid, "status": "pending", "actions": actions})).inserted_id
    a_doc = _run(nx.claim(db, "nl_proposals", {"_id": _id}, from_status="pending"))
    seen = []

    async def _stall(u, act, ctx=None, **kw):
        seen.append(("A", list(ctx["target_ids"])))
        await asyncio.sleep(10)          # never reaches the write

    async def _scenario():
        task = asyncio.create_task(nx.run_claimed(db, "nl_proposals", a_doc, uid, actions, authority=READY, execute_one=_stall))
        await asyncio.sleep(0.05)
        # membership changes: a NEW extreme bot appears; the original bound set must win
        b2 = (await db.bot_configs.insert_one(_bot(uid, "extreme"))).inserted_id
        await db.nl_proposals.update_one({"_id": _id}, {"$set": {
            "execution.lease_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}})
        b_doc = await nx.reclaim_expired(db, "nl_proposals", await db.nl_proposals.find_one({"_id": _id}))

        async def _exec_b(u, act, ctx=None, **kw):
            seen.append(("B", list(ctx["target_ids"])))
            await nx.apply_effect(db, ctx)
            return {"ok": True}
        res = await nx.run_claimed(db, "nl_proposals", b_doc, uid, actions, authority=READY, execute_one=_exec_b)
        task.cancel()
        return res, b2

    res, b2 = _run(_scenario())
    assert res["status"] == "executed"
    assert seen == [("A", [str(b1)]), ("B", [str(b1)])]           # B reused A's bound ids, not b2
    assert str(b2) not in seen[1][1]


def test_production_fails_closed_without_transactions(world, monkeypatch):
    """P0-01: on a standalone MongoDB the fenced boundary REFUSES to write in
    production (RuntimeError, effect row not completed, no domain write)."""
    import nl_execution as nx
    import app_env
    from routes.nl_routes import execute_one
    db, uid = world["db"], world["id"]
    _run(db.trades.insert_one({"user_id": uid, "symbol": "XAUUSD", "status": "open", "action": "BUY", "entry_price": 1.0}))
    hello = _run(db.command("hello"))
    if hello.get("setName"):
        pytest.skip("replica set available — transactions are used here")
    key = "p" * 32
    ctx = {"idempotency_key": key, "execution_id": "ex", "owner": "w1", "fence": 1, "target_ids": None}
    assert _run(nx.reserve_effect(db, key, {"user_id": uid}, ctx=ctx)) == "reserved" and _run(nx.dispatch_effect(db, ctx))
    monkeypatch.setattr(app_env, "is_production", lambda: True)
    with pytest.raises(RuntimeError):
        _run(execute_one(uid, {"type": "CLOSE_ALL_TRADES", "target": "all"}, idem_key=key, ctx=ctx))
    assert _run(db.trades.count_documents({"user_id": uid, "close_requested": True})) == 0
    assert _run(db.nl_effects.find_one({"_id": key}))["state"] == "dispatching"


def test_fenced_commits_effect_and_domain_together(world):
    """The same fenced() call transitions dispatching→applying→completed around
    the domain write; a stale ctx (superseded fence) writes nothing."""
    import nl_execution as nx
    db, uid = world["db"], world["id"]
    key = "q" * 32
    ctx = {"idempotency_key": key, "execution_id": "ex", "owner": "w1", "fence": 1}
    assert _run(nx.reserve_effect(db, key, {"user_id": uid}, ctx=ctx)) == "reserved" and _run(nx.dispatch_effect(db, ctx))
    ctx2 = {**ctx, "owner": "w2", "fence": 2}
    assert _run(nx.reserve_effect(db, key, {"user_id": uid}, ctx=ctx2)) == "reserved"      # takeover
    writes = []

    async def work(session):
        writes.append(1)
        return {"n": 1}
    with pytest.raises(nx.LeaseLost):
        _run(nx.fenced(db, ctx, work))
    assert writes == []
    assert _run(nx.dispatch_effect(db, ctx2)) and _run(nx.fenced(db, ctx2, work)) == {"n": 1}
    assert _run(db.nl_effects.find_one({"_id": key}))["state"] == "completed"


def test_live_suite_is_read_only_by_default():
    sh = open(os.path.join(ROOT, "scripts", "run_full_suite.sh")).read()
    assert 'STOIC_ALLOW_MUTATING_TESTS=${STOIC_ALLOW_MUTATING_TESTS:-YES}' not in sh
    assert 'export STOIC_ALLOW_MUTATING_TESTS=NO' in sh
    mk = open(os.path.join(ROOT, "Makefile")).read()
    assert "STOIC_ALLOW_MUTATING_TESTS=YES ./scripts/run_full_suite.sh" not in mk
    live = open(os.path.join(ROOT, "backend", "tests", "test_iter213_r16_audit.py")).read()
    assert "test_disable_all_bots_flow_and_restore" not in live and "disable all bots" not in live


def test_ea_parses_nl_fence_and_release_admission_pair():
    ea = open(os.path.join(ROOT, "backend", "static", "EmergentTradingBridge.mq5"), encoding="utf-8", errors="ignore").read()
    assert '#property version   "1.57"' in ea and '#define EA_CLIENT_VERSION "1.57"' in ea
    assert '"\\"close_idem_key\\":\\"' in ea and '"\\"close_fence\\":"' in ea
    assert "bool NlCloseAdmitted(" in ea and ea.index("NlCloseAdmitted(trade_id, nl_key, nl_fence)") < ea.index("ClosePosition(trade_id, ticket);")
    assert 'IntentDone("nlkey-" + nl_key)' in ea and "STALE NL fence rejected" in ea
    rel = open(os.path.join(ROOT, ".github", "workflows", "release.yml")).read()
    assert "release-admission.json" in rel and rel.index('"record": "release-admission"') < rel.index("docker buildx imagetools create --tag")
    assert "ALIAS PAIR INCOMPLETE" in rel


def test_notification_survives_crash_after_provider_acceptance(monkeypatch):
    """P2-02: the provider accepts, the process dies before the outbox `sent`
    update; the retry sends with the SAME provider idempotency key (outbox id)."""
    import deploy_watch as dw
    import email_sender
    from database import get_db
    db = get_db()
    calls = []

    async def fake_send(recipient, subject, html, text=None, sender=None, idempotency_key=None):
        calls.append(idempotency_key)
        return {"ok": True, "id": "msg-A"}
    monkeypatch.setattr(email_sender, "send_email", fake_send)
    wid = "watch-crash-test"
    _run(db.deploy_watch.delete_many({"_id": wid}))
    _run(db.deploy_watch_outbox.delete_many({"watch_id": wid}))
    doc = {"_id": wid, "target_url": "https://www.stoicaibot.com", "expected_sha": "abc1234", "notify_email": "ops@example.com",
           "armed_by": "ops@example.com", "armed_at": datetime.now(timezone.utc), "baseline_sha": None}
    _run(db.deploy_watch.insert_one(doc))
    real_ack = dw._outbox_ack

    class Crash(Exception):
        pass

    async def crashing(*a, **k):
        raise Crash()
    monkeypatch.setattr(dw, "_outbox_ack", crashing)
    with pytest.raises(Crash):
        _run(dw._notify(db, doc, "live", {"http": 200, "build_sha": "abc1234"}))
    monkeypatch.setattr(dw, "_outbox_ack", real_ack)
    # claim is fresh (<5 min) → retry is suppressed (deduped) …
    assert _run(dw._notify(db, doc, "live", {"http": 200, "build_sha": "abc1234"}))["deduped"] is True
    # … and a retry after the claim window uses the identical provider key → provider dedupes
    _run(db.deploy_watch_outbox.update_one({"_id": f"{wid}:live"}, {"$set": {"claimed_at": datetime.now(timezone.utc) - timedelta(minutes=6)}}))
    r = _run(dw._notify(db, doc, "live", {"http": 200, "build_sha": "abc1234"}))
    assert r["ok"] and calls == [f"{wid}:live", f"{wid}:live"]
    _run(db.deploy_watch.delete_many({"_id": wid}))
    _run(db.deploy_watch_outbox.delete_many({"watch_id": wid}))
