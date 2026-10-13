"""Item 4 (main132 follow-up): a demo-only production can leave close-only — under an ACTIVE demo_only rules policy
the pre-promotion evidence accepts attested DEMO accounts and skips only the LIVE performance attestation."""
import asyncio
import os
import sys
import types
from datetime import datetime, timezone

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "ops"))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DEMO_POLICY = {"policy_version": "demo-rules-v1", "demo_only": True, "expires_at": "2099-01-01T00:00:00+00:00"}
LIVE_POLICY = {"policy_version": "live-rules-v1", "demo_only": False}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _row(env, attested=None, **kw):
    r = {"enabled": True, "environment": env, "attested_environment": attested or env, "ea_fresh": True, "identity_verified": True,
         "ea_version": "1.64", "open_positions_broker": 0}
    r.update(kw)
    return r


def test_regime_and_environment_gates_pure():
    import prepromotion_evidence as pp
    demo, live, none, expired = (pp.regime_from_policy(DEMO_POLICY), pp.regime_from_policy(LIVE_POLICY), pp.regime_from_policy(None),
                                 pp.regime_from_policy({**DEMO_POLICY, "expired": True}))
    assert demo == {"demo_period": True, "required_environment": "DEMO", "policy_version": "demo-rules-v1", "label": "demo period"}
    assert live["required_environment"] == "LIVE" and none["required_environment"] == "LIVE" and expired["demo_period"] is False
    # demo_only policy + one ATTESTED DEMO account → PASS
    assert pp.environment_gates([_row("DEMO")], demo) == {"all_enabled_attested_demo": True}
    # the same with a LIVE account under demo_only → FAIL
    assert pp.environment_gates([_row("LIVE")], demo) == {"all_enabled_attested_demo": False}
    # a DEMO-looking account WITHOUT a server-side attestation (user-written fields only) → FAIL
    assert pp.environment_gates([_row("DEMO", attested="LIVE")], demo) == {"all_enabled_attested_demo": False}
    assert pp.environment_gates([], demo) == {"all_enabled_attested_demo": False}            # no enabled account never passes
    # LIVE regime unchanged
    assert pp.environment_gates([_row("LIVE")], live) == {"all_enabled_live": True}
    assert pp.environment_gates([_row("DEMO")], live) == {"all_enabled_live": False}


def _wire(monkeypatch, db, policy, attested_env):
    """Everything except the DB-backed account rows is stubbed GREEN so the regime decides the verdict."""
    import prepromotion_evidence as pp

    async def _active(db_):
        return policy
    monkeypatch.setattr("rules_policy.active", _active)
    monkeypatch.setattr("database.get_db", lambda: db)
    monkeypatch.setattr("broker_env.broker_environment", lambda a: "LIVE" if a.get("account_type") == "real" else "DEMO")
    monkeypatch.setattr("broker_env.attested_environment", lambda a: attested_env(a))
    monkeypatch.setattr("synthetic_data.is_synthetic_account", lambda a: False)

    async def _et(db_, account_ids=None):
        return {"unresolved_executions": 0, "position_mismatches": 0}
    monkeypatch.setattr("execution_truth.execution_truth_check", _et)

    async def _auth(db_, acc):
        return {"level": "FULL", "enforced_level": "FULL", "hard_truth_fresh": True, "domains": {}}
    monkeypatch.setattr("trading_authority.compute_authority", _auth)

    async def _contract(db_, uid):
        return {"totals": {"eas_connected": 1, "accounts_enabled": 1}, "rows": [],
                "accounts": [{"account_enabled": True, "ea_connected": True}]}      # v1.60.25 — gate counts enabled rows
    monkeypatch.setattr("state_contract.contract", _contract)
    perf_calls = []

    async def _perf(db_, uid):
        perf_calls.append(uid)
        return ["NOT_LIVE_ENVIRONMENT"]                       # the LIVE-only attestation ALWAYS refuses DEMO rows (as on prod)
    monkeypatch.setattr("routes.performance_routes._attestation_gate", _perf)
    monkeypatch.setattr(pp.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        stdout='{"result":"PASS","signature":"s","totals":{"enabled_ids":["x"]}}', stderr="", returncode=0))
    monkeypatch.setenv("APPROVED_EA_VERSION", "1.64")
    return perf_calls


def _seed(db, account_type):
    oid = ObjectId()
    _run(db.accounts.insert_one({"_id": oid, "user_id": "tenant", "label": "Main", "trading_enabled": True, "account_type": account_type,
                                 "last_heartbeat": datetime.now(timezone.utc).isoformat(), "ea_version": "1.64", "verified_identity": {"ok": True},
                                 "open_positions": 0, "status": "active"}))
    _run(db.bot_configs.insert_one({"account_id": str(oid), "user_id": "tenant", "active": True}))


def test_collect_demo_only_policy_with_attested_demo_account_passes(monkeypatch):
    import prepromotion_evidence as pp
    db = FakeDb()
    _seed(db, "demo")
    perf_calls = _wire(monkeypatch, db, DEMO_POLICY, lambda a: "DEMO")
    body = _run(pp.collect(types.SimpleNamespace(expect="1/1/1", expect_ids=None, scope_user="tenant")))
    assert body["result"] == "PASS", body["failed_gates"]
    assert body["evidence_label"] == "demo period" and body["regime"]["required_environment"] == "DEMO"
    assert body["gates"]["all_enabled_attested_demo"] is True and "all_enabled_live" not in body["gates"]
    assert body["gates"]["performance_reconciled_attestable"] is True and body["performance_gate_reasons"] == ["SKIPPED_DEMO_PERIOD"] and perf_calls == []
    # every other gate is still evaluated and green
    for g in ("ea_heartbeat_fresh_all_enabled", "ea_identity_verified_all_enabled", "zero_unresolved_executions", "zero_position_mismatches",
              "platform_authority_full", "authority_ui_enforced_agree", "hard_truth_fresh_all_enabled", "bot_health_caps_clear", "topology_reconciliation"):
        assert body["gates"][g] is True, g


def test_collect_demo_only_policy_with_live_account_fails(monkeypatch):
    import prepromotion_evidence as pp
    db = FakeDb()
    _seed(db, "real")
    _wire(monkeypatch, db, DEMO_POLICY, lambda a: "LIVE")
    body = _run(pp.collect(types.SimpleNamespace(expect="1/1/1", expect_ids=None, scope_user="tenant")))
    assert body["result"] == "FAIL" and body["failed_gates"] == ["all_enabled_attested_demo"] and body["evidence_label"] == "demo period"


def test_collect_demo_period_still_fails_on_the_other_gates(monkeypatch):
    import prepromotion_evidence as pp
    db = FakeDb()
    _seed(db, "demo")
    _wire(monkeypatch, db, DEMO_POLICY, lambda a: "DEMO")

    async def _et_bad(db_, account_ids=None):
        return {"unresolved_executions": 1, "position_mismatches": 0}
    monkeypatch.setattr("execution_truth.execution_truth_check", _et_bad)
    body = _run(pp.collect(types.SimpleNamespace(expect="1/1/1", expect_ids=None, scope_user="tenant")))
    assert body["result"] == "FAIL" and body["failed_gates"] == ["zero_unresolved_executions"]


def test_collect_live_regime_unchanged(monkeypatch):
    import prepromotion_evidence as pp
    db = FakeDb()
    _seed(db, "real")
    perf_calls = _wire(monkeypatch, db, None, lambda a: "LIVE")
    body = _run(pp.collect(types.SimpleNamespace(expect="1/1/1", expect_ids=None, scope_user="tenant")))
    assert body["evidence_label"] == "live" and body["gates"]["all_enabled_live"] is True
    assert body["gates"]["performance_reconciled_attestable"] is False and perf_calls == ["tenant"]     # LIVE attestation still enforced
    assert body["failed_gates"] == ["performance_reconciled_attestable"]
    upd = open(os.path.join(ROOT, "deploy", "update.sh"), encoding="utf-8").read()
    assert 'evidence_label' in upd and "pre-promotion evidence FAILED" in upd
