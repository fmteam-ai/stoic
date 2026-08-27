"""iter-192 — v54 review batch: flatten escalation ladder, verdict reason
hierarchy, broker adapter certification suite."""
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
ADMIN = ("admin@trading.bot", "admin123")
TIMEOUT = 25


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
              "pamm_nav_snapshots", "pamm_reconciliation", "pamm_audit",
              "pamm_incidents"):
        _run(getattr(db, c).delete_many({"program_id": program_id}))
    _run(db.pamm_events.delete_many({"data.program_id": program_id}))
    _run(db.pamm_notifications.delete_many({"program_id": program_id}))
    if bpid:
        for c in ("sandbox_broker_programs", "sandbox_broker_investors",
                  "sandbox_broker_allocations", "sandbox_broker_positions"):
            _run(getattr(db, c).delete_many({"program_id": bpid}))


class TestEscalationLadder:
    def test_ladder_attempts_and_incident(self):
        from modules.pamm.risk.states import _flatten_and_verify
        s = _login()
        prog = _create_program(s, f"esc-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            # break the broker so every flatten attempt fails
            _run(db.pamm_programs.update_one(
                {"program_id": pid},
                {"$set": {"partner_id": "prt_dead"}}))
            p = _run(db.pamm_programs.find_one({"program_id": pid},
                                               {"_id": 0}))
            # attempt 1 → retry only, no alert, no incident
            _run(_flatten_and_verify(db, p, "test", attempt=1))
            assert _run(db.pamm_notifications.find_one(
                {"program_id": pid})) is None
            assert _run(db.pamm_incidents.find_one(
                {"program_id": pid})) is None
            # attempt 2 → critical alert, still no incident
            p = _run(db.pamm_programs.find_one({"program_id": pid},
                                               {"_id": 0}))
            _run(_flatten_and_verify(db, p, "test", attempt=2))
            note = _run(db.pamm_notifications.find_one(
                {"program_id": pid, "severity": "critical"}))
            assert note is not None
            assert _run(db.pamm_incidents.find_one(
                {"program_id": pid})) is None
            # attempt 3 → formal broker incident opened
            p = _run(db.pamm_programs.find_one({"program_id": pid},
                                               {"_id": 0}))
            _run(_flatten_and_verify(db, p, "test", attempt=3))
            inc = _run(db.pamm_incidents.find_one(
                {"program_id": pid, "status": "open"}))
            assert inc and inc["type"] == "flatten_failed"
            assert _run(db.pamm_events.find_one(
                {"type": "BrokerIncidentOpened",
                 "data.program_id": pid})) is not None
            # attempt 5 → PAGE OPERATOR severity
            p = _run(db.pamm_programs.find_one({"program_id": pid},
                                               {"_id": 0}))
            _run(_flatten_and_verify(db, p, "test", attempt=5))
            page = _run(db.pamm_notifications.find_one(
                {"program_id": pid, "severity": "page"}))
            assert page is not None
            # >5 minutes unresolved → external escalation (backdate first_at)
            _run(db.pamm_programs.update_one(
                {"program_id": pid},
                {"$set": {"flatten_failed.first_at":
                          (datetime.now(timezone.utc)
                           - timedelta(minutes=6)).isoformat()}}))
            p = _run(db.pamm_programs.find_one({"program_id": pid},
                                               {"_id": 0}))
            _run(_flatten_and_verify(db, p, "test", attempt=6))
            inc = _run(db.pamm_incidents.find_one({"program_id": pid}))
            assert inc["external_escalated"] is True
            assert _run(db.pamm_events.find_one(
                {"type": "ExternalEscalation",
                 "data.program_id": pid})) is not None
            # broker heals → resolve closes the incident
            _run(db.pamm_programs.update_one(
                {"program_id": pid},
                {"$set": {"partner_id": "prt_sandbox"}}))
            p = _run(db.pamm_programs.find_one({"program_id": pid},
                                               {"_id": 0}))
            ok = _run(_flatten_and_verify(db, p, "test", attempt=7))
            assert ok is True
            inc = _run(db.pamm_incidents.find_one({"program_id": pid}))
            assert inc["status"] == "resolved"
            assert _run(db.pamm_events.find_one(
                {"type": "BrokerIncidentResolved",
                 "data.program_id": pid})) is not None
        finally:
            _cleanup(pid)

    def test_incidents_endpoint(self):
        s = _login()
        r = s.get(f"{API}/pamm/incidents?status=open", timeout=TIMEOUT)
        assert r.status_code == 200 and "incidents" in r.json()
        r = requests.get(f"{API}/pamm/incidents", timeout=TIMEOUT)
        assert r.status_code in (401, 403)


class TestVerdictReasonHierarchy:
    def test_reduce_names_limiting_factor(self):
        s = _login()
        prog = _create_program(s, f"vrh-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            now = datetime.now(timezone.utc)
            _run(db.pamm_nav_snapshots.insert_many([
                {"program_id": pid, "nav": 100000.0, "currency": "USD",
                 "at": (now - timedelta(days=1)).isoformat()},
                {"program_id": pid, "nav": 96250.0, "currency": "USD",
                 "at": now.isoformat()}]))
            _run(db.pamm_programs.update_one(
                {"program_id": pid},
                {"$set": {"last_nav": {"nav": 96250.0,
                                       "at": now.isoformat()}}}))
            r = s.post(f"{API}/pamm/programs/{pid}/trade-verdict",
                       json={"requested_risk_pct": 0.30}, timeout=TIMEOUT)
            body = r.json()
            assert body["verdict"] == "REDUCE"
            assert body["limiting_factor"] == "daily_loss_pct"
            assert body["primary_reason"] == "daily_loss_pct_headroom"
            assert body["risk_factor"] == 0.5
        finally:
            _cleanup(pid)

    def test_blocked_and_approve_reasons(self):
        s = _login()
        prog = _create_program(s, f"vrh2-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        try:
            r = s.post(f"{API}/pamm/programs/{pid}/trade-verdict",
                       json={"requested_risk_pct": 0.30}, timeout=TIMEOUT)
            assert r.json()["primary_reason"] == "full_headroom"
            s.post(f"{API}/pamm/programs/{pid}/pause",
                   json={"reason": "t"}, timeout=TIMEOUT)
            r = s.post(f"{API}/pamm/programs/{pid}/trade-verdict",
                       json={"requested_risk_pct": 0.30}, timeout=TIMEOUT)
            body = r.json()
            assert body["verdict"] == "REJECT"
            assert body["limiting_factor"] == "trading_blocked"
            assert body["risk_factor"] == 0.0
        finally:
            _cleanup(pid)


class TestCertificationSuite:
    def test_sandbox_adapter_certifies_100(self):
        s = _login()
        r = s.post(f"{API}/pamm/partners/prt_sandbox/certify",
                   timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["certified"] is True, body
        assert body["score"] == 100.0
        assert body["failed"] == 0 and body["passed"] >= 10
        names = {c["check"] for c in body["results"]}
        for required in ("authentication", "master_identity", "nav",
                         "positions", "investor_and_allocation", "pause",
                         "resume", "close_all", "stale_event_rejected",
                         "duplicate_event_idempotent",
                         "unknown_program_rejected"):
            assert required in names, f"missing check {required}"
        # persisted on the partner + visible in health overview
        r = s.get(f"{API}/pamm/health", timeout=TIMEOUT)
        sbx = next(p for p in r.json()["partners"]
                   if p["partner_id"] == "prt_sandbox")
        assert sbx["certification"]["certified"] is True

    def test_broken_adapter_fails_certification(self):
        from services.broker_gateway.certification import certify_adapter
        db = _db()
        fake = {"partner_id": f"prt_cert_{uuid.uuid4().hex[:6]}",
                "name": "Broken", "adapter": "no-such-adapter",
                "webhook_secret_enc": None}
        _run(db.broker_partners.insert_one(dict(fake)))
        try:
            report = _run(certify_adapter(db, fake))
            assert report["certified"] is False
            assert report["failed"] >= 1
        finally:
            _run(db.broker_partners.delete_many(
                {"partner_id": fake["partner_id"]}))

    def test_certify_endpoint_admin_only(self):
        r = requests.post(f"{API}/pamm/partners/prt_sandbox/certify",
                          timeout=TIMEOUT)
        assert r.status_code in (401, 403)
        s = _login()
        r = s.post(f"{API}/pamm/partners/prt_nope/certify", timeout=TIMEOUT)
        assert r.status_code == 404
