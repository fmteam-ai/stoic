"""iter-188 — PAMM Strategy Marketplace (publish + join funnel) and
automatic risk sweeps (60s loop: risk engine + broker heartbeat)."""
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
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"
TIMEOUT = 25


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _admin():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


def _investor_user():
    """Register a throwaway verified user and return (session, email)."""
    email = f"pamm-inv-{uuid.uuid4().hex[:8]}@example.com"
    pw = f"Vx!{uuid.uuid4().hex[:12]}9"
    s = requests.Session()
    r = s.post(f"{API}/auth/register",
               json={"email": email, "password": pw, "name": "Test Investor",
                     "terms_agreed": True}, timeout=TIMEOUT)
    assert r.status_code in (200, 201), r.text
    _run(_db().users.update_one({"email": email},
                                {"$set": {"email_verified": True}}))
    r = s.post(f"{API}/auth/login",
               json={"email": email, "password": pw}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s, email


def _create_program(s, name):
    r = s.post(f"{API}/pamm/programs", json={"name": name}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()


def _cleanup(program_id, emails=()):
    db = _db()
    p = _run(db.pamm_programs.find_one({"program_id": program_id})) or {}
    bpid = p.get("broker_program_id")
    for c in ("pamm_programs", "pamm_master_accounts", "pamm_allocations",
              "pamm_nav_snapshots", "pamm_reconciliation", "pamm_audit",
              "pamm_join_requests"):
        _run(getattr(db, c).delete_many({"program_id": program_id}))
    _run(db.pamm_events.delete_many({"data.program_id": program_id}))
    _run(db.pamm_notifications.delete_many({"program_id": program_id}))
    if bpid:
        for c in ("sandbox_broker_programs", "sandbox_broker_investors",
                  "sandbox_broker_allocations", "sandbox_broker_positions"):
            _run(getattr(db, c).delete_many({"program_id": bpid}))
    for e in emails:
        _run(db.users.delete_many({"email": e}))
        _run(db.notifications.delete_many(
            {"kind": {"$regex": "^pamm_join"},
             "user_id": {"$exists": True}}))


class TestPublishAndListings:
    def test_publish_flow_and_visibility(self):
        admin = _admin()
        inv, email = _investor_user()
        prog = _create_program(admin, f"mkt-pub-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        try:
            # unpublished → invisible to investors
            r = inv.get(f"{API}/pamm/marketplace", timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            assert pid not in [x["program_id"] for x in r.json()["listings"]]
            # publish with a pitch
            r = admin.post(f"{API}/pamm/programs/{pid}/publish",
                           json={"publish": True, "pitch": "Steady gold alpha"},
                           timeout=TIMEOUT)
            assert r.status_code == 200 and r.json()["published"] is True
            r = inv.get(f"{API}/pamm/marketplace", timeout=TIMEOUT)
            listing = next(x for x in r.json()["listings"]
                           if x["program_id"] == pid)
            assert listing["pitch"] == "Steady gold alpha"
            assert "performance" in listing
            # no internal fields leak
            for banned in ("manager_id", "broker_program_id", "risk_limits",
                           "risk_breach"):
                assert banned not in listing
            # unpublish → gone again
            admin.post(f"{API}/pamm/programs/{pid}/publish",
                       json={"publish": False}, timeout=TIMEOUT)
            r = inv.get(f"{API}/pamm/marketplace", timeout=TIMEOUT)
            assert pid not in [x["program_id"] for x in r.json()["listings"]]
        finally:
            _cleanup(pid, [email])

    def test_publish_requires_manager(self):
        admin = _admin()
        inv, email = _investor_user()
        prog = _create_program(admin, f"mkt-perm-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        try:
            r = inv.post(f"{API}/pamm/programs/{pid}/publish",
                         json={"publish": True}, timeout=TIMEOUT)
            assert r.status_code == 403, r.text
        finally:
            _cleanup(pid, [email])


class TestJoinFunnel:
    def test_full_request_approve_flow(self):
        admin = _admin()
        prog = _create_program(admin, f"mkt-join-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        admin.post(f"{API}/pamm/programs/{pid}/publish",
                   json={"publish": True}, timeout=TIMEOUT)
        inv, email = _investor_user()
        try:
            # invalid amount rejected
            r = inv.post(f"{API}/pamm/marketplace/{pid}/join",
                         json={"amount": -5}, timeout=TIMEOUT)
            assert r.status_code == 400
            # valid request
            r = inv.post(f"{API}/pamm/marketplace/{pid}/join",
                         json={"amount": 2500, "note": "long-term"},
                         timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            rid = r.json()["request_id"]
            assert r.json()["status"] == "pending"
            # duplicate pending blocked
            r = inv.post(f"{API}/pamm/marketplace/{pid}/join",
                         json={"amount": 100}, timeout=TIMEOUT)
            assert r.status_code == 400
            # visible in my-requests
            r = inv.get(f"{API}/pamm/marketplace/my-requests",
                        timeout=TIMEOUT)
            assert rid in [x["request_id"] for x in r.json()["requests"]]
            # investor cannot see the manager queue / cannot decide
            r = inv.get(f"{API}/pamm/programs/{pid}/join-requests",
                        timeout=TIMEOUT)
            assert r.status_code == 403
            r = inv.post(f"{API}/pamm/join-requests/{rid}/approve",
                         timeout=TIMEOUT)
            assert r.status_code == 403
            # manager queue shows it
            r = admin.get(f"{API}/pamm/programs/{pid}/join-requests",
                          timeout=TIMEOUT)
            assert rid in [x["request_id"] for x in r.json()["requests"]]
            # approve → allocation created broker-side + mirrored
            r = admin.post(f"{API}/pamm/join-requests/{rid}/approve",
                           timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "approved"
            db = _db()
            alloc = _run(db.pamm_allocations.find_one(
                {"program_id": pid, "amount": 2500.0}))
            assert alloc is not None
            req = _run(db.pamm_join_requests.find_one({"request_id": rid}))
            assert req["status"] == "approved" and req.get("investor_id")
            ev = _run(db.pamm_events.find_one(
                {"type": "JoinApproved", "data.request_id": rid}))
            assert ev is not None
            note = _run(db.notifications.find_one(
                {"kind": "pamm_join_approved", "user_id": req["user_id"]}))
            assert note is not None
            # double-decide blocked
            r = admin.post(f"{API}/pamm/join-requests/{rid}/reject",
                           timeout=TIMEOUT)
            assert r.status_code == 409
        finally:
            _cleanup(pid, [email])

    def test_reject_flow_and_unpublished_join_404(self):
        admin = _admin()
        prog = _create_program(admin, f"mkt-rej-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        inv, email = _investor_user()
        try:
            # unpublished → join is 404
            r = inv.post(f"{API}/pamm/marketplace/{pid}/join",
                         json={"amount": 100}, timeout=TIMEOUT)
            assert r.status_code == 404
            admin.post(f"{API}/pamm/programs/{pid}/publish",
                       json={"publish": True}, timeout=TIMEOUT)
            r = inv.post(f"{API}/pamm/marketplace/{pid}/join",
                         json={"amount": 100}, timeout=TIMEOUT)
            rid = r.json()["request_id"]
            r = admin.post(f"{API}/pamm/join-requests/{rid}/reject",
                           timeout=TIMEOUT)
            assert r.status_code == 200 and r.json()["status"] == "rejected"
            # no allocation was made
            assert _run(_db().pamm_allocations.find_one(
                {"program_id": pid})) is None
            ev = _run(_db().pamm_events.find_one(
                {"type": "JoinRejected", "data.request_id": rid}))
            assert ev is not None
        finally:
            _cleanup(pid, [email])


class TestAutoSweep:
    def test_sweep_once_enforces_and_records(self):
        from modules.pamm.sweep import sweep_once
        admin = _admin()
        prog = _create_program(admin, f"swp-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            now = datetime.now(timezone.utc)
            _run(db.pamm_nav_snapshots.insert_many([
                {"program_id": pid, "nav": 100000.0, "currency": "USD",
                 "at": (now - timedelta(days=2)).isoformat()},
                {"program_id": pid, "nav": 90000.0, "currency": "USD",
                 "at": now.isoformat()}]))
            _run(db.pamm_programs.update_one(
                {"program_id": pid},
                {"$set": {"last_nav": {"nav": 90000.0,
                                       "at": now.isoformat()}}}))
            res = _run(sweep_once(db))
            assert res["programs_checked"] >= 1
            hit = next((b for b in res["breaches"]
                        if b["program_id"] == pid), None)
            assert hit is not None and "daily_loss_pct" in hit["limits"]
            p = _run(db.pamm_programs.find_one({"program_id": pid}))
            assert p["trading"] == "paused"
            assert p["risk_breach"]["actor"] == "auto-sweep"
            # heartbeats ran too
            sbx = next(h for h in res["heartbeats"]
                       if h["partner_id"] == "prt_sandbox")
            assert sbx["ok"] is True
            # last-sweep doc + endpoint
            last = _run(db.pamm_sweeps.find_one({"_id": "last"}))
            assert last and last["at"] == res["at"]
            r = admin.get(f"{API}/pamm/sweep-status", timeout=TIMEOUT)
            assert r.status_code == 200 and r.json().get("at")
            # second sweep is idempotent (breach already recorded)
            res2 = _run(sweep_once(db))
            assert not any(b["program_id"] == pid for b in res2["breaches"])
        finally:
            _cleanup(pid)

    def test_sweep_loop_registered(self):
        import background_loops
        assert hasattr(background_loops, "_pamm_sweep_loop")
        # health retention cleanup keeps only recent entries
        db = _db()
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        _run(db.pamm_health.insert_one(
            {"partner_id": "prt_retention_test", "ok": True,
             "latency_ms": 1, "at": old}))
        from modules.pamm.sweep import sweep_once
        _run(sweep_once(db))
        assert _run(db.pamm_health.find_one(
            {"partner_id": "prt_retention_test"})) is None

    def test_sweep_status_requires_manager(self):
        r = requests.get(f"{API}/pamm/sweep-status", timeout=TIMEOUT)
        assert r.status_code in (401, 403)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
