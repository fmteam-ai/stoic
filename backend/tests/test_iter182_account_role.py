"""iter-182 — PAMM account-role model end-to-end.

1. AccountCreate accepts account_role + pamm_* fields; STANDARD normalizes
   pamm fields to None; API returns the role.
2. submit_intent REFUSES PAMM_INVESTOR before minting any intent
   (account_role_locked) and REFUSES an unbound PAMM_MASTER
   (pamm_master_unbound).
3. Live activation readiness reports the investor monitor-only problem.
"""
import asyncio
import os
import uuid

import requests
from pymongo import MongoClient

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
ADMIN_EMAIL = os.environ.get("STOIC_TEST_ADMIN_EMAIL", "admin@stoicaibot.com")
ADMIN_PWD = os.environ.get("STOIC_TEST_ADMIN_PASSWORD", "admin123")

TAG = uuid.uuid4().hex[:8]


def _admin():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PWD}, timeout=30)
    assert r.status_code == 200, r.text[:200]
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


def _mk(s, role, **extra):
    payload = {"label": f"TEST_role_{role}_{TAG}", "broker": "TESTBRK",
               "server": "test-server", "account_number": f"9{TAG[:6]}",
               "account_type": "demo", "account_role": role,
               "mode": "paper", "initial_balance": 1000, **extra}
    r = s.post(f"{BASE}/api/accounts", json=payload, timeout=30)
    assert r.status_code == 200, r.text[:300]
    return r.json()


def _cleanup():
    db = MongoClient(MONGO_URL)[DB_NAME]
    db.accounts.delete_many({"label": {"$regex": f"^TEST_role_.*_{TAG}$"}})


class _NeverEngine:
    async def execute_authorized(self, **_kw):
        raise AssertionError("engine must NEVER be reached")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_role_persisted_and_standard_normalized():
    s = _admin()
    try:
        inv = _mk(s, "PAMM_INVESTOR", pamm_provider="brokerdesk",
                  pamm_program_id="prog-x", pamm_broker_program_id="bp-1")
        assert inv["account_role"] == "PAMM_INVESTOR"
        assert inv["pamm_provider"] == "brokerdesk"
        std = _mk(s, "STANDARD", pamm_provider="should-be-dropped")
        assert std["account_role"] == "STANDARD"
        assert std.get("pamm_provider") is None
    finally:
        _cleanup()


def test_investor_execution_locked_no_intent_minted():
    async def scenario():
        from motor.motor_asyncio import AsyncIOMotorClient
        from execution_authority import submit_intent
        db = AsyncIOMotorClient(MONGO_URL)[DB_NAME]
        before = await db.execution_intents.count_documents({})
        res = await submit_intent(
            user_id="_t182", engine=_NeverEngine(),
            account={"_id": f"_t182acc_{TAG}", "account_number": "1", "trading_enabled": True,
                     "server": "s", "account_role": "PAMM_INVESTOR"},
            signal={"symbol": "EURUSD", "action": "BUY", "lot_size": 0.01})
        after = await db.execution_intents.count_documents({})
        return res, before, after
    res, before, after = _run(scenario())
    assert res.get("blocked") == "account_role_locked", res
    assert "MONITOR ONLY" in res.get("reason", "")
    assert after == before, "monitor-only account minted an execution intent"


def test_pamm_master_unbound_refused():
    async def scenario():
        from motor.motor_asyncio import AsyncIOMotorClient
        from execution_authority import submit_intent
        db = AsyncIOMotorClient(MONGO_URL)[DB_NAME]
        return await submit_intent(
            user_id="_t182", engine=_NeverEngine(),
            account={"_id": f"_t182m_{TAG}", "account_number": "2", "trading_enabled": True,
                     "server": "s", "account_role": "PAMM_MASTER"},
            signal={"symbol": "EURUSD", "action": "BUY", "lot_size": 0.01})
    res = _run(scenario())
    assert res.get("blocked") == "pamm_master_unbound", res


def test_activation_readiness_reports_investor_monitor_only():
    async def scenario():
        from motor.motor_asyncio import AsyncIOMotorClient
        from routes.bot_routes import _activation_readiness
        db = AsyncIOMotorClient(MONGO_URL)[DB_NAME]
        return await _activation_readiness(
            db, {"mode": "live", "status": "connected",
                 "account_role": "PAMM_INVESTOR"})
    problems = _run(scenario())
    assert any("PAMM_INVESTOR" in p and "monitor only" in p
               for p in problems), problems
