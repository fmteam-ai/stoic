"""iter-189 — PAMM Batch 1 (external review §10-12):
trade verdict APPROVE/REDUCE/REJECT, emergency op-state hierarchy,
dual authorization for critical changes."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
ADMIN_A = ("admin@trading.bot", "admin123")
ADMIN_B = ("admin@stoicaibot.com", "admin123")
TIMEOUT = 25


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _login(creds):
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": creds[0], "password": creds[1]},
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
              "pamm_nav_snapshots", "pamm_reconciliation", "pamm_audit",
              "pamm_change_requests"):
        _run(getattr(db, c).delete_many({"program_id": program_id}))
    _run(db.pamm_events.delete_many({"data.program_id": program_id}))
    _run(db.pamm_notifications.delete_many({"program_id": program_id}))
    if bpid:
        for c in ("sandbox_broker_programs", "sandbox_broker_investors",
                  "sandbox_broker_allocations", "sandbox_broker_positions"):
            _run(getattr(db, c).delete_many({"program_id": bpid}))


def _seed_navs(program_id, navs):
    db = _db()
    _run(db.pamm_nav_snapshots.insert_many(
        [{"program_id": program_id, "nav": float(n), "currency": "USD",
          "at": d.isoformat()} for n, d in navs]))
    last = max(navs, key=lambda x: x[1])
    _run(db.pamm_programs.update_one(
        {"program_id": program_id},
        {"$set": {"last_nav": {"nav": float(last[0]),
                               "at": last[1].isoformat()}}}))


class TestTradeVerdict:
    def test_approve_reduce_reject_spectrum(self):
        s = _login(ADMIN_A)
        prog = _create_program(s, f"vrd-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        try:
            now = datetime.now(timezone.utc)
            # fresh program, no losses → full APPROVE
            _seed_navs(pid, [(100000, now - timedelta(days=1)),
                             (100000, now)])
            r = s.post(f"{API}/pamm/programs/{pid}/trade-verdict",
                       json={"requested_risk_pct": 0.30}, timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            assert r.json()["verdict"] == "APPROVE"
            assert r.json()["approved_risk_pct"] == 0.30
            # 75% of the daily cap used (3.75% of 5%) → REDUCE to ~half
            _run(_db().pamm_nav_snapshots.delete_many({"program_id": pid}))
            _seed_navs(pid, [(100000, now - timedelta(days=1)),
                             (96250, now)])
            r = s.post(f"{API}/pamm/programs/{pid}/trade-verdict",
                       json={"requested_risk_pct": 0.30}, timeout=TIMEOUT)
            body = r.json()
            assert body["verdict"] == "REDUCE", body
            assert 0 < body["approved_risk_pct"] < 0.30
            assert any(f["limit"] == "daily_loss_pct" for f in body["factors"])
            # ≥95% of cap used → REJECT (factor ≤ 0.1)
            _run(_db().pamm_nav_snapshots.delete_many({"program_id": pid}))
            _seed_navs(pid, [(100000, now - timedelta(days=1)),
                             (95200, now)])
            r = s.post(f"{API}/pamm/programs/{pid}/trade-verdict",
                       json={"requested_risk_pct": 0.30}, timeout=TIMEOUT)
            assert r.json()["verdict"] == "REJECT"
            assert r.json()["approved_risk_pct"] == 0.0
            # invalid request
            r = s.post(f"{API}/pamm/programs/{pid}/trade-verdict",
                       json={"requested_risk_pct": -1}, timeout=TIMEOUT)
            assert r.status_code == 400
        finally:
            _cleanup(pid)

    def test_risk_reduced_state_halves_risk(self):
        s = _login(ADMIN_A)
        prog = _create_program(s, f"vrd-rr-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        try:
            now = datetime.now(timezone.utc)
            _seed_navs(pid, [(100000, now)])
            r = s.post(f"{API}/pamm/programs/{pid}/op-state",
                       json={"state": "risk_reduced", "reason": "test"},
                       timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            r = s.post(f"{API}/pamm/programs/{pid}/trade-verdict",
                       json={"requested_risk_pct": 0.40}, timeout=TIMEOUT)
            body = r.json()
            assert body["verdict"] == "REDUCE"
            assert body["approved_risk_pct"] == 0.20
        finally:
            _cleanup(pid)


class TestOpStateHierarchy:
    def test_escalation_deescalation_and_locked(self):
        s = _login(ADMIN_A)
        prog = _create_program(s, f"ops-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            # escalate running → close_risk_only (instant, pauses broker)
            r = s.post(f"{API}/pamm/programs/{pid}/op-state",
                       json={"state": "close_risk_only"}, timeout=TIMEOUT)
            assert r.status_code == 200 and r.json()["op_state"] == "close_risk_only"
            p = _run(db.pamm_programs.find_one({"program_id": pid}))
            assert p["trading"] == "paused"
            r = s.get(f"{API}/pamm/programs/{pid}", timeout=TIMEOUT)
            assert r.json()["trading_allowed"] is False
            assert r.json()["trading_block_reason"] == "close_risk_only"
            # resume endpoint refuses above new_trades_paused
            r = s.post(f"{API}/pamm/programs/{pid}/resume", timeout=TIMEOUT)
            assert r.status_code == 409
            # de-escalate to running via op-state (step-up bypassed in tests)
            r = s.post(f"{API}/pamm/programs/{pid}/op-state",
                       json={"state": "running"}, timeout=TIMEOUT)
            assert r.status_code == 200
            p = _run(db.pamm_programs.find_one({"program_id": pid}))
            assert p["trading"] == "enabled" and p["op_state"] == "running"
            # escalate to locked → estop flag set, everything blocked
            r = s.post(f"{API}/pamm/programs/{pid}/op-state",
                       json={"state": "locked"}, timeout=TIMEOUT)
            assert r.status_code == 200
            p = _run(db.pamm_programs.find_one({"program_id": pid}))
            assert p["emergency_stop"] is True
            r = s.get(f"{API}/pamm/programs/{pid}", timeout=TIMEOUT)
            assert r.json()["trading_block_reason"] == "locked"
            # human de-escalation from LOCKED refused → dual auth only
            r = s.post(f"{API}/pamm/programs/{pid}/op-state",
                       json={"state": "running"}, timeout=TIMEOUT)
            assert r.status_code == 409
            r = s.post(f"{API}/pamm/programs/{pid}/clear-emergency-stop",
                       timeout=TIMEOUT)
            assert r.status_code == 409
            # OpStateChanged events recorded
            ev = _run(db.pamm_events.find_one(
                {"type": "OpStateChanged", "data.program_id": pid,
                 "data.to": "locked"}))
            assert ev is not None
        finally:
            _cleanup(pid)

    def test_automation_cannot_deescalate(self):
        from modules.pamm.risk.states import set_op_state
        s = _login(ADMIN_A)
        prog = _create_program(s, f"ops-auto-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            s.post(f"{API}/pamm/programs/{pid}/op-state",
                   json={"state": "new_trades_paused"}, timeout=TIMEOUT)
            p = _run(db.pamm_programs.find_one({"program_id": pid},
                                               {"_id": 0}))
            import pytest
            with pytest.raises(PermissionError):
                _run(set_op_state(db, p, "running", "ai-bot",
                                  source="risk-engine",
                                  allow_deescalate=True))
        finally:
            _cleanup(pid)

    def test_emergency_stop_maps_to_flatten_state(self):
        s = _login(ADMIN_A)
        prog = _create_program(s, f"ops-es-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        try:
            r = s.post(f"{API}/pamm/programs/{pid}/emergency-stop",
                       json={"reason": "test"}, timeout=TIMEOUT)
            assert r.status_code == 200
            assert r.json()["op_state"] == "emergency_flatten"
            # clear-estop de-escalates to new_trades_paused
            r = s.post(f"{API}/pamm/programs/{pid}/clear-emergency-stop",
                       timeout=TIMEOUT)
            assert r.status_code == 200
            p = _run(_db().pamm_programs.find_one({"program_id": pid}))
            assert p["op_state"] == "new_trades_paused"
            assert p["emergency_stop"] is False
        finally:
            _cleanup(pid)

    def test_risk_breach_escalates_op_state(self):
        s = _login(ADMIN_A)
        prog = _create_program(s, f"ops-br-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        try:
            now = datetime.now(timezone.utc)
            _seed_navs(pid, [(100000, now - timedelta(days=2)),
                             (90000, now)])
            r = s.post(f"{API}/pamm/programs/{pid}/risk-check",
                       timeout=TIMEOUT)
            assert r.json()["action_taken"] == "halt"
            p = _run(_db().pamm_programs.find_one({"program_id": pid}))
            assert p["op_state"] == "new_trades_paused"
        finally:
            _cleanup(pid)


class TestDualAuthorization:
    def test_loosening_limits_needs_second_admin(self):
        a = _login(ADMIN_A)
        b = _login(ADMIN_B)
        prog = _create_program(a, f"da-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            # tightening applies immediately
            r = a.put(f"{API}/pamm/programs/{pid}/risk-limits",
                      json={"daily_loss_pct": {"threshold": 3.0}},
                      timeout=TIMEOUT)
            assert r.status_code == 200
            assert not r.json().get("pending_approval")
            # loosening → pending change request
            r = a.put(f"{API}/pamm/programs/{pid}/risk-limits",
                      json={"daily_loss_pct": {"threshold": 8.0}},
                      timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            assert r.json()["pending_approval"] is True
            cid = r.json()["change_id"]
            # limit unchanged until approval
            p = _run(db.pamm_programs.find_one({"program_id": pid}))
            assert p["risk_limits"]["daily_loss_pct"]["threshold"] == 3.0
            # SAME admin cannot approve their own request
            r = a.post(f"{API}/pamm/change-requests/{cid}/approve",
                       timeout=TIMEOUT)
            assert r.status_code == 403, r.text
            # duplicate pending of same kind blocked
            r = a.put(f"{API}/pamm/programs/{pid}/risk-limits",
                      json={"weekly_loss_pct": {"threshold": 50.0}},
                      timeout=TIMEOUT)
            assert r.status_code == 409
            # DIFFERENT admin approves → applied + events + audit
            r = b.post(f"{API}/pamm/change-requests/{cid}/approve",
                       timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            p = _run(db.pamm_programs.find_one({"program_id": pid}))
            assert p["risk_limits"]["daily_loss_pct"]["threshold"] == 8.0
            ev = _run(db.pamm_events.find_one(
                {"type": "ChangeApproved", "data.change_id": cid}))
            assert ev is not None
            # double-decide → 409
            r = b.post(f"{API}/pamm/change-requests/{cid}/reject",
                       timeout=TIMEOUT)
            assert r.status_code == 409
        finally:
            _cleanup(pid)

    def test_unlock_locked_requires_dual_auth(self):
        a = _login(ADMIN_A)
        b = _login(ADMIN_B)
        prog = _create_program(a, f"da-lk-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            a.post(f"{API}/pamm/programs/{pid}/op-state",
                   json={"state": "locked"}, timeout=TIMEOUT)
            # create unlock change request
            r = a.post(f"{API}/pamm/programs/{pid}/change-requests",
                       json={"kind": "unlock",
                             "payload": {"target": "new_trades_paused"},
                             "reason": "incident resolved"}, timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            cid = r.json()["change_id"]
            # second admin approves → program leaves LOCKED
            r = b.post(f"{API}/pamm/change-requests/{cid}/approve",
                       timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            p = _run(db.pamm_programs.find_one({"program_id": pid}))
            assert p["op_state"] == "new_trades_paused"
            assert p["emergency_stop"] is False
        finally:
            _cleanup(pid)

    def test_reject_leaves_state_untouched(self):
        a = _login(ADMIN_A)
        b = _login(ADMIN_B)
        prog = _create_program(a, f"da-rj-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        try:
            r = a.put(f"{API}/pamm/programs/{pid}/risk-limits",
                      json={"max_drawdown_pct": {"enabled": False}},
                      timeout=TIMEOUT)
            cid = r.json()["change_id"]
            r = b.post(f"{API}/pamm/change-requests/{cid}/reject",
                       timeout=TIMEOUT)
            assert r.status_code == 200
            p = _run(_db().pamm_programs.find_one({"program_id": pid}))
            limits = p.get("risk_limits") or {}
            assert limits.get("max_drawdown_pct", {}).get(
                "enabled", True) is True
        finally:
            _cleanup(pid)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
