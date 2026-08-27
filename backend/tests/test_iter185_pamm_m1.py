"""iter-185 — PAMM Milestone 1: broker gateway (sandbox adapter), PAMM
module, event stream, webhooks (signed/replay/idempotent), reconciliation.
"""
import json
import os
import sys
import time
import uuid

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


def _cleanup(program_id):
    db = _db()
    p = _run(db.pamm_programs.find_one({"program_id": program_id})) or {}
    bpid = p.get("broker_program_id")
    for c in ("pamm_programs", "pamm_master_accounts", "pamm_allocations",
              "pamm_nav_snapshots", "pamm_reconciliation", "pamm_audit"):
        _run(getattr(db, c).delete_many({"program_id": program_id}))
    if bpid:
        for c in ("sandbox_broker_programs", "sandbox_broker_investors",
                  "sandbox_broker_allocations"):
            _run(getattr(db, c).delete_many({"program_id": bpid}))


def _create_program(s, name):
    r = s.post(f"{API}/pamm/programs", json={"name": name}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()


def test_pamm_program_lifecycle_end_to_end():
    """create → investor+allocation (broker authoritative) → nav →
    reconcile → pause/resume → emergency stop."""
    s = _admin()
    pg = _create_program(s, f"iter185-{uuid.uuid4().hex[:6]}")
    pid = pg["program_id"]
    try:
        assert pg["broker_program_id"].startswith("sbx_")

        r = s.post(f"{API}/pamm/programs/{pid}/investors",
                   json={"name": "Test Investor", "amount": 25000},
                   timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        inv = r.json()
        assert inv["allocation"]["amount"] == 25000

        r = s.post(f"{API}/pamm/programs/{pid}/reconcile", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        rec = r.json()
        assert rec["broker_nav"] > 0
        assert rec["allocations_match"] is True

        r = s.get(f"{API}/pamm/programs/{pid}", timeout=TIMEOUT)
        detail = r.json()
        assert detail["program"]["last_nav"]["nav"] == rec["broker_nav"]
        assert detail["trading_allowed"] is True

        # pause blocks trading; resume restores
        assert s.post(f"{API}/pamm/programs/{pid}/pause",
                      json={"reason": "test"}, timeout=TIMEOUT).status_code == 200
        detail = s.get(f"{API}/pamm/programs/{pid}", timeout=TIMEOUT).json()
        assert detail["trading_allowed"] is False
        assert s.post(f"{API}/pamm/programs/{pid}/resume",
                      timeout=TIMEOUT).status_code == 200

        # emergency stop: blocks, resume refused until admin clears
        assert s.post(f"{API}/pamm/programs/{pid}/emergency-stop",
                      json={"reason": "drill"}, timeout=TIMEOUT).status_code == 200
        assert s.post(f"{API}/pamm/programs/{pid}/resume",
                      timeout=TIMEOUT).status_code == 409
        assert s.post(f"{API}/pamm/programs/{pid}/clear-emergency-stop",
                      timeout=TIMEOUT).status_code == 200
    finally:
        _cleanup(pid)


def test_pamm_event_stream_records_lifecycle():
    s = _admin()
    pg = _create_program(s, f"iter185-ev-{uuid.uuid4().hex[:6]}")
    pid = pg["program_id"]
    try:
        s.post(f"{API}/pamm/programs/{pid}/pause", json={"reason": "x"},
               timeout=TIMEOUT)
        r = s.get(f"{API}/pamm/events", params={"program_id": pid},
                  timeout=TIMEOUT)
        types = [e["type"] for e in r.json()["events"]]
        assert "ProgramCreated" in types
        assert "StrategyPaused" in types
    finally:
        _run(_db().pamm_events.delete_many({"data.program_id": pid}))
        _cleanup(pid)


def test_pamm_permissions():
    # anonymous → 401/403
    r = requests.get(f"{API}/pamm/programs", timeout=TIMEOUT)
    assert r.status_code in (401, 403)
    # non-admin/non-manager account cannot list
    db = _db()
    email = f"iter185-{uuid.uuid4().hex[:8]}@test.io"
    s2 = requests.Session()
    r = s2.post(f"{API}/auth/register",
                json={"email": email, "password": "Str0ng!pass123",
                      "terms_agreed": True}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    # activate directly in DB so we can login (bypass email verify)
    _run(db.users.update_one({"email": email},
                             {"$set": {"email_verified": True,
                                       "status": "active"}}))
    r = s2.post(f"{API}/auth/login",
                json={"email": email, "password": "Str0ng!pass123"},
                timeout=TIMEOUT)
    if r.status_code == 200:
        r = s2.get(f"{API}/pamm/programs", timeout=TIMEOUT)
        assert r.status_code == 403
        # admin grants manager role → access opens
        u = _run(db.users.find_one({"email": email}))
        s = _admin()
        r = s.post(f"{API}/pamm/managers",
                   json={"user_id": str(u["_id"]), "grant": True},
                   timeout=TIMEOUT)
        assert r.status_code == 200
        r = s2.get(f"{API}/pamm/programs", timeout=TIMEOUT)
        assert r.status_code == 200
        assert r.json()["programs"] == []   # owns none
    _run(db.users.delete_many({"email": email}))


def test_pamm_webhook_signature_replay_and_idempotency():
    from services.broker_gateway.auth import sign_payload
    from services.broker_gateway.pamm_api import (get_partner,
                                                  webhook_secret)
    db = _db()
    secret = webhook_secret(_run(get_partner(db, "prt_sandbox")))
    event_id = f"whk_{uuid.uuid4().hex[:10]}"
    body = json.dumps({"type": "NAVUpdated", "event_id": event_id,
                       "data": {"program_id": "pgm_x", "nav": 1}}).encode()
    ts = str(time.time())
    url = f"{API}/pamm/webhooks/prt_sandbox"
    hdr = {"Content-Type": "application/json",
           "X-Broker-Timestamp": ts,
           "X-Broker-Signature": sign_payload(secret, ts, body)}
    try:
        r = requests.post(url, data=body, headers=hdr, timeout=TIMEOUT)
        assert r.status_code == 200 and r.json()["duplicate"] is False
        # idempotent redelivery
        r = requests.post(url, data=body, headers=hdr, timeout=TIMEOUT)
        assert r.status_code == 200 and r.json()["duplicate"] is True
        # bad signature
        bad = dict(hdr, **{"X-Broker-Signature": "0" * 64})
        assert requests.post(url, data=body, headers=bad,
                             timeout=TIMEOUT).status_code == 401
        # stale timestamp (replay)
        old_ts = str(time.time() - 9999)
        stale = {"Content-Type": "application/json",
                 "X-Broker-Timestamp": old_ts,
                 "X-Broker-Signature": sign_payload(secret, old_ts, body)}
        assert requests.post(url, data=body, headers=stale,
                             timeout=TIMEOUT).status_code == 401
    finally:
        _run(db.pamm_events.delete_many(
            {"event_key": f"prt_sandbox:{event_id}"}))
        _run(db.pamm_notifications.delete_many({"partner_id": "prt_sandbox"}))


def test_reconciliation_detects_allocation_drift():
    s = _admin()
    pg = _create_program(s, f"iter185-drift-{uuid.uuid4().hex[:6]}")
    pid = pg["program_id"]
    db = _db()
    try:
        s.post(f"{API}/pamm/programs/{pid}/investors",
               json={"name": "I", "amount": 10000}, timeout=TIMEOUT)
        # corrupt the STOIC mirror — broker stays authoritative
        _run(db.pamm_allocations.update_one(
            {"program_id": pid}, {"$set": {"amount": 5000.0}}))
        r = s.post(f"{API}/pamm/programs/{pid}/reconcile", timeout=TIMEOUT)
        rec = r.json()
        assert rec["allocations_match"] is False
        assert rec["status"] == "drift"
        ev = _run(db.pamm_events.find_one(
            {"type": "ReconciliationDrift", "data.program_id": pid}))
        assert ev is not None
    finally:
        _run(db.pamm_events.delete_many({"data.program_id": pid}))
        _cleanup(pid)
