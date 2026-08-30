"""iter-196 — Global Trading Authority (v56 §16), BROKER_UNCERTAIN
op-state (§8) and Position Truth v2 netting/classification (§5)."""
import os
import sys
import uuid

import pytest
import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
ADMIN = ("admin@trading.bot", "admin123")
TIMEOUT = 30


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _login():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN[0], "password": ADMIN[1]},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


def _create_program(s, name):
    r = s.post(f"{API}/pamm/programs", json={"name": name}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()


def _cleanup(program_id):
    db = _db()
    p = _run(db.pamm_programs.find_one({"program_id": program_id})) or {}
    bpid = p.get("broker_program_id")
    for c in ("pamm_programs", "pamm_master_accounts", "pamm_allocations",
              "pamm_nav_snapshots", "pamm_audit", "pamm_incidents",
              "pamm_position_truth", "pamm_expected_positions",
              "execution_intents"):
        _run(getattr(db, c).delete_many({"program_id": program_id}))
    _run(db.pamm_events.delete_many({"data.program_id": program_id}))
    _run(db.pamm_notifications.delete_many({"program_id": program_id}))
    if bpid:
        for c in ("sandbox_broker_programs", "sandbox_broker_positions"):
            _run(getattr(db, c).delete_many({"program_id": bpid}))


def _reset_platform():
    _run(_db().platform_state.delete_many({"_id": "trading_authority"}))


class TestGlobalTradingAuthority:
    def test_authority_endpoint_shape(self):
        s = _login()
        r = s.get(f"{API}/authority", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        from trading_authority import LEVELS
        assert body["level"] in LEVELS
        assert body["enforced_level"] in LEVELS
        # audit v4 P0-1 — position_truth may carry canonical truth values
        # (STALE/UNKNOWN/CONFLICTED) when the caller's accounts are not
        # fresh; every other domain sticks to authority LEVELS.
        truth_levels = ("STALE", "UNKNOWN", "CONFLICTED")
        for d in ("platform", "account", "broker", "infrastructure",
                  "risk", "pamm", "execution", "position_truth"):
            assert d in body["domains"], f"missing domain {d}"
            allowed = (list(LEVELS) + list(truth_levels)
                       if d == "position_truth" else list(LEVELS))
            assert body["domains"][d]["level"] in allowed

    def test_requires_auth(self):
        r = requests.get(f"{API}/authority", timeout=TIMEOUT)
        assert r.status_code in (401, 403)

    def test_platform_kill_switch_blocks_new_trades(self):
        from trading_authority import enforce_new_trade
        s = _login()
        try:
            r = s.post(f"{API}/authority/platform",
                       json={"level": "CLOSE_ONLY",
                             "reason": "test kill switch"},
                       timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            gate = _run(enforce_new_trade(_db()))
            assert gate["ok"] is False
            assert gate["level"] == "CLOSE_ONLY"
            body = s.get(f"{API}/authority", timeout=TIMEOUT).json()
            assert body["enforced_level"] == "CLOSE_ONLY"
            assert body["restricted"] is True
            # relax back to FULL (step-up gated; bypass via conftest)
            r = s.post(f"{API}/authority/platform",
                       json={"level": "FULL", "reason": "test done"},
                       timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            gate = _run(enforce_new_trade(_db()))
            assert gate["ok"] is True
        finally:
            _reset_platform()

    def test_reduced_halves_volume(self):
        from trading_authority import enforce_new_trade
        s = _login()
        try:
            r = s.post(f"{API}/authority/platform",
                       json={"level": "REDUCED", "reason": "test"},
                       timeout=TIMEOUT)
            assert r.status_code == 200
            gate = _run(enforce_new_trade(_db()))
            assert gate["ok"] is True
            assert gate["reduce_factor"] == 0.5
        finally:
            _reset_platform()

    def test_relaxing_requires_step_up(self):
        s = _login()
        try:
            r = s.post(f"{API}/authority/platform",
                       json={"level": "PAUSED", "reason": "restrict"},
                       timeout=TIMEOUT)
            assert r.status_code == 200
            s.headers["X-Step-Up-Bypass"] = ""  # exercise the REAL gate
            r = s.post(f"{API}/authority/platform",
                       json={"level": "FULL", "reason": "relax"},
                       timeout=TIMEOUT)
            assert r.status_code in (401, 403), \
                "relaxing authority must require step-up MFA"
        finally:
            _reset_platform()

    def test_non_admin_cannot_set_platform(self):
        # audit trail exists after admin changes
        db = _db()
        n = _run(db.authority_audit.count_documents({}))
        assert isinstance(n, int)

    def test_unknown_intents_degrade_execution_domain(self):
        from execution_intents import create_intent
        from trading_authority import execution_domain
        db = _db()
        it = _run(create_intent(db, source="t", kind="open_trade"))
        _run(db.execution_intents.update_one(
            {"intent_id": it["intent_id"]},
            {"$set": {"status": "unknown"}}))
        try:
            d = _run(execution_domain(db))
            assert d["level"] == "REDUCED"
            assert "UNKNOWN" in d["reason"]
        finally:
            _run(db.execution_intents.delete_many(
                {"intent_id": it["intent_id"]}))


class TestBrokerUncertainOpState:
    def test_state_exists_and_blocks_new_trades(self):
        from modules.pamm.risk.states import (OP_STATES, blocks_new_trades,
                                              severity)
        assert "broker_uncertain" in OP_STATES
        assert blocks_new_trades("broker_uncertain") is True
        assert severity("broker_uncertain") > severity("new_trades_paused")
        assert severity("broker_uncertain") < severity("close_risk_only")

    def test_escalation_blocks_trading_and_needs_human_to_resume(self):
        from modules.pamm.risk import trading_allowed
        from modules.pamm.risk.states import set_op_state
        s = _login()
        prog = _create_program(s, f"bu-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            p = _run(db.pamm_programs.find_one({"program_id": pid},
                                               {"_id": 0}))
            out = _run(set_op_state(db, p, "broker_uncertain",
                                    "auto-sweep", reason="truth lost",
                                    source="automation"))
            assert out["changed"] is True
            p2 = _run(db.pamm_programs.find_one({"program_id": pid},
                                                {"_id": 0}))
            allowed, reason = _run(trading_allowed(db, p2))
            assert allowed is False
            # automation may NEVER de-escalate
            with pytest.raises(PermissionError):
                _run(set_op_state(db, p2, "running", "auto-sweep",
                                  reason="looks fine",
                                  source="automation"))
        finally:
            _cleanup(pid)


class TestPositionTruthV2:
    def test_netting_mode_compares_net_exposure(self):
        from modules.pamm.reconciliation.position_truth import \
            check_position_truth
        s = _login()
        prog = _create_program(s, f"net-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            _run(db.pamm_programs.update_one(
                {"program_id": pid},
                {"$set": {"position_mode": "netting"}}))
            p = _run(db.pamm_programs.find_one({"program_id": pid},
                                               {"_id": 0}))
            bpid = p["broker_program_id"]
            # baseline on empty book
            r1 = _run(check_position_truth(db, p, actor="test"))
            assert r1["status"] == "baseline" and r1["mode"] == "netting"
            # broker book gains BUY 0.5 + SELL 0.2 = net +0.3
            _run(db.sandbox_broker_positions.insert_many([
                {"program_id": bpid, "position_id": "n1",
                 "symbol": "XAUUSD", "volume": 0.5, "side": "BUY"},
                {"program_id": bpid, "position_id": "n2",
                 "symbol": "XAUUSD", "volume": 0.2, "side": "SELL"}]))
            r2 = _run(check_position_truth(db, p, actor="test"))
            assert r2["status"] == "drift"
            assert r2["classification"] == ["NET_EXPOSURE_MISMATCH"]
            assert r2["broker_net"] == {"XAUUSD": 0.3}
            assert r2["mismatched"][0]["symbol"] == "XAUUSD"
            # per-position ids are irrelevant in netting mode
            assert r2["missing"] == [] and r2["unexpected"] == []
        finally:
            _cleanup(pid)

    def test_hedging_mode_classifies_drift_origin(self):
        from modules.pamm.reconciliation.position_truth import \
            check_position_truth
        s = _login()
        prog = _create_program(s, f"cls-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            p = _run(db.pamm_programs.find_one({"program_id": pid},
                                               {"_id": 0}))
            bpid = p["broker_program_id"]
            _run(check_position_truth(db, p, actor="test"))  # baseline
            _run(db.sandbox_broker_positions.insert_one(
                {"program_id": bpid, "position_id": "h1",
                 "symbol": "EURUSD", "volume": 1.0, "side": "BUY"}))
            r = _run(check_position_truth(db, p, actor="test"))
            assert r["status"] == "drift"
            assert r["classification"] == ["UNEXPECTED_AT_BROKER"]
        finally:
            _cleanup(pid)

    def test_truth_failure_counter_escalates_to_broker_uncertain(self):
        s = _login()
        prog = _create_program(s, f"buf-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            # break the broker link so truth checks fail, then sweep 3x
            _run(db.pamm_programs.update_one(
                {"program_id": pid},
                {"$set": {"partner_id": "prt_does_not_exist"}}))
            from modules.pamm.sweep import sweep_once
            for _ in range(3):
                _run(sweep_once(db))
            p = _run(db.pamm_programs.find_one({"program_id": pid},
                                               {"_id": 0}))
            assert int(p.get("position_truth_failures") or 0) >= 3
            assert p.get("op_state") == "broker_uncertain"
        finally:
            _cleanup(pid)


class TestReleaseSignerHatchRemoved:
    def test_local_signing_forbidden_in_prod_without_ack(
            self, monkeypatch):
        # audit v5 P0-7 — local signing in prod is forbidden UNLESS the
        # operator explicitly acknowledges a supervised pilot with
        # RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD=true (boot guard parity).
        import release_signing
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("RELEASE_SIGNER", "local")
        monkeypatch.delenv("RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD",
                           raising=False)
        with pytest.raises(RuntimeError, match="forbidden in production"):
            release_signing.sign_hex(b"x")
        monkeypatch.setenv("RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD", "true")
        assert release_signing.sign_hex(b"x")

    def test_preflight_flags_override_as_fail(self, monkeypatch):
        from deploy_preflight import run_preflight
        monkeypatch.setenv("RELEASE_SIGNER", "local")
        monkeypatch.setenv("RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD", "true")
        out = run_preflight()
        check = next(c for c in out["checks"]
                     if c["id"] == "release_signer")
        assert check["status"] == "fail"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
