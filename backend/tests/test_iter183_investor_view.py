"""iter-183 — PAMM Investor Mirroring View (read-only).

1. A user with a PAMM_INVESTOR account linked via pamm_program_id sees the
   program in /pamm/investor/programs and the detail view is redacted
   (no risk_limits / governance / manager_id) + Execution Authority LOCKED.
2. An approved marketplace join request ALSO links the program and the
   investor's allocations price the mirrored share.
3. A stranger gets 404 on the detail view (no enumeration).
"""
import os
import uuid

import requests
from pymongo import MongoClient

from helpers import mark_email_verified

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
ADMIN_EMAIL = os.environ.get("STOIC_TEST_ADMIN_EMAIL", "admin@stoicaibot.com")
ADMIN_PWD = os.environ.get("STOIC_TEST_ADMIN_PASSWORD", "admin123")
TAG = uuid.uuid4().hex[:8]
PWD = "Kd5#Zt9mW2xVpR7c"


def _db():
    return MongoClient(MONGO_URL)[DB_NAME]


def _login(email, pwd):
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": email, "password": pwd},
               timeout=30)
    assert r.status_code == 200, r.text[:200]
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


def _register(email):
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/register",
               json={"email": email, "password": PWD, "name": "QA",
                     "terms_agreed": True}, timeout=30)
    assert r.status_code == 200, r.text[:200]
    mark_email_verified(email)
    return _login(email, PWD)


def _seed_program():
    db = _db()
    pid = f"pgm_test{TAG}"
    db.pamm_programs.insert_one({
        "program_id": pid, "broker_program_id": f"bp_{TAG}",
        "partner_id": "prt_sandbox", "name": f"TEST Mirror {TAG}",
        "currency": "USD", "manager_id": "000000000000000000000000",
        "manager_fee_pct": 20.0, "status": "active", "trading": "enabled",
        "emergency_stop": False, "investor_count": 1, "aum": 121000.0,
        "last_nav": {"nav": 121000.0, "at": "2026-03-01T00:00:00+00:00"},
        "risk_limits": {"secret": 1}, "governance": {"secret": 1},
        "op_state": "running", "created_at": "2026-01-01T00:00:00+00:00"})
    db.pamm_nav_snapshots.insert_many([
        {"program_id": pid, "nav": 100000.0, "at": "2026-01-01T00:00:00+00:00"},
        {"program_id": pid, "nav": 110000.0, "at": "2026-02-01T00:00:00+00:00"},
        {"program_id": pid, "nav": 121000.0, "at": "2026-03-01T00:00:00+00:00"}])
    db.trades.insert_one({
        "pamm_program_id": pid, "user_id": "000000000000000000000000",
        "account_id": f"acc_{TAG}", "symbol": "XAUUSD", "action": "BUY",
        "lot_size": 0.1, "pnl": 42.0, "status": "closed",
        "entry_price": 4000.0, "exit_price": 4004.2,
        "opened_at": "2026-02-10T00:00:00+00:00",
        "closed_at": "2026-02-10T01:00:00+00:00", "label": f"TEST_{TAG}"})
    return pid


def _cleanup():
    db = _db()
    pid = f"pgm_test{TAG}"
    db.pamm_programs.delete_many({"program_id": pid})
    db.pamm_nav_snapshots.delete_many({"program_id": pid})
    db.pamm_allocations.delete_many({"program_id": pid})
    db.pamm_join_requests.delete_many({"program_id": pid})
    db.trades.delete_many({"pamm_program_id": pid})
    db.accounts.delete_many({"label": {"$regex": f"^TEST_inv_{TAG}"}})
    users = list(db.users.find({"email": {"$regex": f"{TAG}@example.com$"}},
                               {"_id": 1}))
    for u in users:
        db.accounts.delete_many({"user_id": str(u["_id"])})
    db.users.delete_many({"email": {"$regex": f"{TAG}@example.com$"}})


def test_investor_mirroring_view():
    pid = _seed_program()
    try:
        inv = _register(f"inv_{TAG}@example.com")
        stranger = _register(f"str_{TAG}@example.com")

        # nothing linked yet → empty, never an error
        r = inv.get(f"{BASE}/api/pamm/investor/programs", timeout=30)
        assert r.status_code == 200 and r.json()["programs"] == []
        assert r.json()["read_only"] is True

        # 1. link via PAMM_INVESTOR account (pamm_program_id)
        r = inv.post(f"{BASE}/api/accounts", json={
            "label": f"TEST_inv_{TAG}", "broker": "TESTBRK",
            "server": "test-server", "account_number": f"7{TAG[:6]}",
            "account_type": "demo", "account_role": "PAMM_INVESTOR",
            "pamm_provider": "sandbox", "pamm_program_id": pid,
            "mode": "paper", "initial_balance": 1000}, timeout=30)
        assert r.status_code == 200, r.text[:300]

        r = inv.get(f"{BASE}/api/pamm/investor/programs", timeout=30)
        rows = r.json()["programs"]
        assert len(rows) == 1 and rows[0]["program"]["program_id"] == pid
        assert rows[0]["sources"] == ["account"]
        assert rows[0]["performance"]["total_return_pct"] == 21.0
        assert rows[0]["share"]["share_pct"] is None  # no allocation yet

        # 2. approved marketplace join → allocation prices the share
        db = _db()
        uid = str(db.users.find_one({"email": f"inv_{TAG}@example.com"})["_id"])
        db.pamm_join_requests.insert_one({
            "request_id": f"jrq_{TAG}", "program_id": pid, "user_id": uid,
            "status": "approved", "investor_id": f"inv_{TAG}",
            "amount": 10000.0, "at": "2026-01-15T00:00:00+00:00"})
        db.pamm_allocations.insert_one({
            "program_id": pid, "investor_id": f"inv_{TAG}",
            "broker_allocation_id": f"alc_{TAG}", "amount": 10000.0,
            "at": "2026-01-15T00:00:00+00:00"})

        r = inv.get(f"{BASE}/api/pamm/investor/programs/{pid}", timeout=30)
        assert r.status_code == 200, r.text[:300]
        v = r.json()
        assert sorted(v["sources"]) == ["account", "marketplace"]
        assert v["share"]["share_pct"] == 10.0
        assert v["share"]["estimated_value"] == 12100.0
        assert v["share"]["mirrored_pnl"] == 2100.0
        assert v["share"]["estimated"] is True
        assert v["execution_authority"]["locked"] is True
        assert "MONITOR ONLY" in v["execution_authority"]["reason"]
        assert v["trading_allowed"] is True
        assert len(v["nav"]) == 3 and v["nav"][0]["nav"] == 100000.0
        assert v["master_trades"]["recent_closed"][0]["pnl"] == 42.0
        assert len(v["my_allocations"]) == 1
        assert len(v["linked_accounts"]) == 1
        # redaction
        for k in ("risk_limits", "governance", "manager_id"):
            assert k not in v["program"]

        # 3. stranger → 404 (no enumeration), admin → allowed
        r = stranger.get(f"{BASE}/api/pamm/investor/programs/{pid}", timeout=30)
        assert r.status_code == 404
        r = _login(ADMIN_EMAIL, ADMIN_PWD).get(
            f"{BASE}/api/pamm/investor/programs/{pid}", timeout=30)
        assert r.status_code == 200 and r.json()["sources"] == ["admin"]
    finally:
        _cleanup()
