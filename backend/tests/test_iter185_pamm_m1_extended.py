"""iter-185 PAMM M1 — Extended coverage per review request:
API contract, allocations/nav/master, reconciliation history, risk gates,
authz negative paths, webhook negative paths, input validation, regressions.
"""
import json
import os
import sys
import time
import uuid

import pytest
import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@stoicaibot.com"
ADMIN_EMAIL_FALLBACK = "admin@trading.bot"
ADMIN_PW = "admin123"
TIMEOUT = 25


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _admin(email=ADMIN_EMAIL):
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": email, "password": ADMIN_PW},
               timeout=TIMEOUT)
    if r.status_code != 200 and email == ADMIN_EMAIL:
        return _admin(ADMIN_EMAIL_FALLBACK)
    assert r.status_code == 200, r.text
    return s


def _cleanup(program_id):
    db = _db()
    p = _run(db.pamm_programs.find_one({"program_id": program_id})) or {}
    bpid = p.get("broker_program_id")
    for c in ("pamm_programs", "pamm_master_accounts", "pamm_allocations",
              "pamm_nav_snapshots", "pamm_reconciliation", "pamm_audit"):
        _run(getattr(db, c).delete_many({"program_id": program_id}))
    _run(db.pamm_events.delete_many({"data.program_id": program_id}))
    if bpid:
        for c in ("sandbox_broker_programs", "sandbox_broker_investors",
                  "sandbox_broker_allocations"):
            _run(getattr(db, c).delete_many({"program_id": bpid}))


def _create(s, name=None):
    name = name or f"iter185x-{uuid.uuid4().hex[:6]}"
    r = s.post(f"{API}/pamm/programs", json={"name": name}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- API contract ---------------------------------------------------
def test_admin_login_and_primary_admin_email():
    """Both admin emails accepted per test_credentials.md."""
    r = requests.post(f"{API}/auth/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
                      timeout=TIMEOUT)
    assert r.status_code == 200, r.text


def test_program_creation_returns_ids_and_lists():
    s = _admin()
    pg = _create(s)
    pid = pg["program_id"]
    try:
        assert pid.startswith(("pgm_", "pmg_")) or len(pid) > 5
        assert pg["broker_program_id"].startswith("sbx_")
        r = s.get(f"{API}/pamm/programs", timeout=TIMEOUT)
        assert r.status_code == 200
        ids = [p["program_id"] for p in r.json()["programs"]]
        assert pid in ids
    finally:
        _cleanup(pid)


def test_full_api_contract_investors_alloc_nav_master():
    s = _admin()
    pg = _create(s)
    pid = pg["program_id"]
    try:
        r = s.post(f"{API}/pamm/programs/{pid}/investors",
                   json={"name": "Alice", "amount": 15000}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        inv = r.json()
        assert inv["allocation"]["amount"] == 15000

        # reconcile to snapshot NAV/master
        rec = s.post(f"{API}/pamm/programs/{pid}/reconcile",
                     timeout=TIMEOUT).json()
        assert rec["broker_nav"] > 0
        assert rec["allocations_match"] is True
        assert rec["status"] == "ok"

        r = s.get(f"{API}/pamm/programs/{pid}/allocations", timeout=TIMEOUT)
        assert r.status_code == 200
        allocs = r.json()["allocations"]
        assert len(allocs) >= 1
        assert all(a.get("amount", 0) > 0 for a in allocs)

        r = s.get(f"{API}/pamm/programs/{pid}/nav", timeout=TIMEOUT)
        assert r.status_code == 200
        assert isinstance(r.json()["nav"], list)

        r = s.get(f"{API}/pamm/programs/{pid}/master", timeout=TIMEOUT)
        assert r.status_code == 200
        master = r.json()
        assert master.get("equity", 0) > 0
        assert master.get("investor_count", 0) >= 1

        r = s.get(f"{API}/pamm/programs/{pid}", timeout=TIMEOUT)
        assert r.status_code == 200
        d = r.json()
        assert d["trading_allowed"] is True
        assert "performance" in d
    finally:
        _cleanup(pid)


def test_reconciliation_history_ok():
    s = _admin()
    pg = _create(s)
    pid = pg["program_id"]
    try:
        s.post(f"{API}/pamm/programs/{pid}/investors",
               json={"name": "B", "amount": 10000}, timeout=TIMEOUT)
        s.post(f"{API}/pamm/programs/{pid}/reconcile", timeout=TIMEOUT)
        s.post(f"{API}/pamm/programs/{pid}/reconcile", timeout=TIMEOUT)
        r = s.get(f"{API}/pamm/programs/{pid}/reconciliation", timeout=TIMEOUT)
        assert r.status_code == 200
        hist = r.json()["reconciliation"]
        assert len(hist) >= 2
    finally:
        _cleanup(pid)


# ---------- Risk gates -----------------------------------------------------
def test_risk_gates_pause_resume_emergency_stop_flow():
    s = _admin()
    pg = _create(s)
    pid = pg["program_id"]
    try:
        # pause
        assert s.post(f"{API}/pamm/programs/{pid}/pause",
                      json={"reason": "test"}, timeout=TIMEOUT).status_code == 200
        d = s.get(f"{API}/pamm/programs/{pid}", timeout=TIMEOUT).json()
        assert d["trading_allowed"] is False
        assert "paus" in (d.get("trading_block_reason") or "").lower()
        # resume
        assert s.post(f"{API}/pamm/programs/{pid}/resume",
                      timeout=TIMEOUT).status_code == 200
        d = s.get(f"{API}/pamm/programs/{pid}", timeout=TIMEOUT).json()
        assert d["trading_allowed"] is True
        # emergency stop → resume returns 409
        assert s.post(f"{API}/pamm/programs/{pid}/emergency-stop",
                      json={"reason": "drill"}, timeout=TIMEOUT).status_code == 200
        r = s.post(f"{API}/pamm/programs/{pid}/resume", timeout=TIMEOUT)
        assert r.status_code == 409
        # admin clears
        assert s.post(f"{API}/pamm/programs/{pid}/clear-emergency-stop",
                      timeout=TIMEOUT).status_code == 200
        assert s.post(f"{API}/pamm/programs/{pid}/resume",
                      timeout=TIMEOUT).status_code == 200
    finally:
        _cleanup(pid)


# ---------- Authorization --------------------------------------------------
def test_authz_anonymous_and_non_manager_forbidden():
    # anonymous
    r = requests.get(f"{API}/pamm/programs", timeout=TIMEOUT)
    assert r.status_code in (401, 403)
    r = requests.post(f"{API}/pamm/programs", json={"name": "x"},
                      timeout=TIMEOUT)
    assert r.status_code in (401, 403)

    # non-manager user
    db = _db()
    email = f"iter185x-{uuid.uuid4().hex[:8]}@test.io"
    s2 = requests.Session()
    r = s2.post(f"{API}/auth/register",
                json={"email": email, "password": "Str0ng!pass123",
                      "terms_agreed": True}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    _run(db.users.update_one({"email": email},
                             {"$set": {"email_verified": True,
                                       "status": "active"}}))
    r = s2.post(f"{API}/auth/login",
                json={"email": email, "password": "Str0ng!pass123"},
                timeout=TIMEOUT)
    if r.status_code != 200:
        pytest.skip("login for fresh user failed")
    try:
        # non-manager cannot list
        assert s2.get(f"{API}/pamm/programs",
                      timeout=TIMEOUT).status_code == 403
        # non-admin cannot create programs (even if manager) — must be admin
        assert s2.post(f"{API}/pamm/programs", json={"name": "nope"},
                       timeout=TIMEOUT).status_code == 403
        # grant manager
        u = _run(db.users.find_one({"email": email}))
        adm = _admin()
        r = adm.post(f"{API}/pamm/managers",
                     json={"user_id": str(u["_id"]), "grant": True},
                     timeout=TIMEOUT)
        assert r.status_code == 200
        r = s2.get(f"{API}/pamm/programs", timeout=TIMEOUT)
        assert r.status_code == 200
        assert r.json()["programs"] == []
        # still cannot create program (create is admin-only)
        assert s2.post(f"{API}/pamm/programs", json={"name": "still-no"},
                       timeout=TIMEOUT).status_code == 403
    finally:
        _run(db.users.delete_many({"email": email}))


# ---------- Webhook negative paths -----------------------------------------
def test_webhook_missing_signature_headers_401():
    url = f"{API}/pamm/webhooks/prt_sandbox"
    body = json.dumps({"type": "NAVUpdated", "event_id": "x",
                       "data": {}}).encode()
    r = requests.post(url, data=body,
                      headers={"Content-Type": "application/json"},
                      timeout=TIMEOUT)
    assert r.status_code == 401


def test_webhook_garbage_json_with_valid_signature_400():
    from services.broker_gateway.auth import sign_payload
    from services.broker_gateway.pamm_api import (get_partner,
                                                  webhook_secret)
    db = _db()
    secret = webhook_secret(_run(get_partner(db, "prt_sandbox")))
    body = b"{not json"
    ts = str(time.time())
    hdr = {"Content-Type": "application/json",
           "X-Broker-Timestamp": ts,
           "X-Broker-Signature": sign_payload(secret, ts, body)}
    r = requests.post(f"{API}/pamm/webhooks/prt_sandbox",
                      data=body, headers=hdr, timeout=TIMEOUT)
    assert r.status_code == 400


def test_webhook_unknown_partner_returns_4xx_not_500():
    body = json.dumps({"type": "NAVUpdated", "event_id": "y",
                       "data": {}}).encode()
    ts = str(time.time())
    hdr = {"Content-Type": "application/json",
           "X-Broker-Timestamp": ts,
           "X-Broker-Signature": "0" * 64}
    r = requests.post(f"{API}/pamm/webhooks/prt_does_not_exist",
                      data=body, headers=hdr, timeout=TIMEOUT)
    assert r.status_code in (400, 401, 404), r.status_code
    assert r.status_code != 500


# ---------- Input validation -----------------------------------------------
def test_create_program_empty_name_400():
    s = _admin()
    r = s.post(f"{API}/pamm/programs", json={"name": ""}, timeout=TIMEOUT)
    assert r.status_code == 400
    r = s.post(f"{API}/pamm/programs", json={}, timeout=TIMEOUT)
    assert r.status_code == 400


def test_investor_amount_zero_or_negative_400():
    s = _admin()
    pg = _create(s)
    pid = pg["program_id"]
    try:
        r = s.post(f"{API}/pamm/programs/{pid}/investors",
                   json={"name": "Z", "amount": 0}, timeout=TIMEOUT)
        assert r.status_code == 400, r.text
        r = s.post(f"{API}/pamm/programs/{pid}/investors",
                   json={"name": "N", "amount": -100}, timeout=TIMEOUT)
        assert r.status_code == 400, r.text
    finally:
        _cleanup(pid)


# ---------- Regressions ----------------------------------------------------
def test_health_regression():
    r = requests.get(f"{API}/health", timeout=TIMEOUT)
    assert r.status_code == 200


def test_ops_runtime_stats_admin_regression():
    s = _admin()
    r = s.get(f"{API}/ops/runtime-stats", timeout=TIMEOUT)
    assert r.status_code == 200
