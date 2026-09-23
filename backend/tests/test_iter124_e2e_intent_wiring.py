"""iter-124 · Prove the real trade-creation path routes through the
new authority pipeline: after POST /api/accounts/{id}/test-trade the
persisted trade has execution_intent_id, and the referenced intent has
history containing created → validated → authorized → submitted."""
from __future__ import annotations
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import requests
from bson import ObjectId
from live_target import require_live_base_url

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_TESTS_DIR)
_REPO_DIR = os.path.dirname(_BACKEND_DIR)
sys.path.insert(0, _BACKEND_DIR)

BASE_URL = require_live_base_url()
TIMEOUT = 30


def _mongo():
    from pymongo import MongoClient
    with open(os.path.join(_BACKEND_DIR, ".env")) as f:
        cfg = {ln.split("=", 1)[0]: ln.split("=", 1)[1].strip().strip("\"'")
               for ln in f if "=" in ln}
    return MongoClient(cfg["MONGO_URL"])[cfg["DB_NAME"]]


def _register_verified():
    suffix = uuid.uuid4().hex[:10]
    email, pw = f"iter124_{suffix}@example.com", "Gy6#Vb3kM9zRnD2s"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": pw,
                            "name": f"iter124-{suffix}",
                            "terms_agreed": True}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    _mongo().users.update_one(
        {"_id": ObjectId(uid)},
        {"$set": {"email_verified": True},
         "$unset": {"activation_token": "", "activation_expires_at": ""}})
    s = requests.Session()
    s.post(f"{BASE_URL}/api/auth/login",
           json={"email": email, "password": pw}, timeout=TIMEOUT)
    valid = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    _mongo().subscriptions.update_one(
        {"user_id": uid},
        {"$set": {"current_plan_id": "elite_ai_monthly",
                  "valid_until": valid}}, upsert=True)
    return uid, s


def _add_and_seed(sess):
    r = sess.post(f"{BASE_URL}/api/accounts", json={
        "label": f"iter124-{uuid.uuid4().hex[:6]}", "broker": "STARTRADER",
        "server": "T-Demo",
        "account_number": "iter124-" + uuid.uuid4().hex[:8],
        "account_type": "demo", "base_currency": "USD", "mode": "live",
    }, timeout=TIMEOUT)
    aid = r.json()["id"]
    inst_id = f"inst_iter124_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    acc = _mongo().accounts.find_one({"_id": ObjectId(aid)})
    from routes.diagnostic_routes import LATEST_EA
    specs = {s: {"stops_level_points": 10, "freeze_level_points": 0}
             for s in ("XAUUSD", "EURUSD", "BTCUSD")}
    _mongo().accounts.update_one({"_id": ObjectId(aid)}, {"$set": {
        "status": "connected", "last_heartbeat": now.isoformat(),
        "trading_enabled": True,     # audit P1-6: execution requires the explicit flag
        "ea_version": LATEST_EA,
        "account_type": "hedging",  # EA-reported margin mode (cert check)
        "available_symbols": ["XAUUSD", "EURUSD", "BTCUSD"],
        "balance": 10000.0, "equity": 10000.0,
        "symbol_specs": specs,
        "symbol_specs_updated_at": now.isoformat(),
        "spreads_updated_at": now.isoformat(),
        "broker_utc_offset_sec": 0,
        "last_full_sync_at": now.isoformat(),
        "ea_identity": {"installation_id": inst_id,
                        "authoritative": True, "ea_version": LATEST_EA,
                        "verified_at": now.isoformat()}}})
    _mongo().installations.insert_one({
        "installation_id": inst_id, "user_id": acc["user_id"],
        "account_id": aid, "terminal_path": "C:/mt5",
        "host_fingerprint": "iter124", "revoked": False, "created_at": now})
    _mongo().execution_leases.update_one(
        {"account_id": aid},
        {"$set": {"installation_id": inst_id, "user_id": acc["user_id"],
                  "revoked": False,
                  "expires_at": now + timedelta(seconds=120)}}, upsert=True)
    return aid


@pytest.fixture
def cleanup():
    refs = {"user_ids": [], "account_ids": []}
    yield refs
    db = _mongo()
    if refs["user_ids"]:
        db.users.delete_many({"_id": {"$in": refs["user_ids"]}})
    if refs["account_ids"]:
        ids_str = [str(a) for a in refs["account_ids"]]
        db.accounts.delete_many({"_id": {"$in": refs["account_ids"]}})
        db.trades.delete_many({"account_id": {"$in": ids_str}})
        db.execution_intents.delete_many({"account_id": {"$in": ids_str}})


def test_trade_persists_execution_intent_id_with_full_history(cleanup):
    uid, sess = _register_verified()
    aid = _add_and_seed(sess)
    cleanup["user_ids"].append(ObjectId(uid))
    cleanup["account_ids"].append(ObjectId(aid))

    r = sess.post(f"{BASE_URL}/api/accounts/{aid}/test-trade",
                  timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    trade_id = r.json()["trade_id"]

    trade = _mongo().trades.find_one({"_id": ObjectId(trade_id)})
    assert trade is not None
    intent_id = trade.get("execution_intent_id")
    assert intent_id, (
        f"trade must carry execution_intent_id; got keys={list(trade)}")

    intent = _mongo().execution_intents.find_one({"intent_id": intent_id})
    assert intent is not None, f"intent {intent_id} missing"
    history = [h["to"] for h in intent.get("history", [])]
    for expected in ("created", "validated", "authorized", "submitted"):
        assert expected in history, (
            f"missing {expected} in intent history {history}")


def test_authority_and_intents_admin_endpoints(cleanup):
    """Regression: GET /api/authority and /api/execution/intents/... are 200
    for admin."""
    sess = requests.Session()
    r = sess.post(f"{BASE_URL}/api/auth/login",
                  json={"email": "admin@trading.bot",
                        "password": "admin123"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    r1 = sess.get(f"{BASE_URL}/api/authority", timeout=TIMEOUT)
    assert r1.status_code == 200, r1.text
    # execution intents list route (per iter193 tests it is /stats + list)
    r2 = sess.get(f"{BASE_URL}/api/execution/intents/stats", timeout=TIMEOUT)
    assert r2.status_code == 200, r2.text
    r3 = sess.get(f"{BASE_URL}/api/execution/intents?limit=5",
                  timeout=TIMEOUT)
    assert r3.status_code == 200, r3.text


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
