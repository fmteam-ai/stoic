"""iter-86 · Test-trade endpoint validates the per-account execution pipe.

Covers:
  · POST /api/accounts/{id}/test-trade returns 200 + trade_id on a healthy
    account with fresh heartbeat + tradeable symbol.
  · Persisted trade is tagged `is_test=true` + `origin="test_trade"`.
  · Paper accounts → 400 paper_account.
  · Stale-EA (heartbeat > 2 min old) → 409 stale_ea.
  · Other-user's account → 403.
  · Broker with no tradeable base symbol in MarketWatch → 409 no_tradeable_symbol.
"""
from __future__ import annotations
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import requests
from bson import ObjectId
from pymongo import MongoClient
from live_target import require_live_base_url

BASE_URL = require_live_base_url()
TIMEOUT = 30


def _mongo():
    with open(_os.path.join(_BACKEND_DIR, ".env")) as f:
        cfg = {ln.split("=", 1)[0]: ln.split("=", 1)[1].strip().strip("\"'")
               for ln in f if "=" in ln}
    return MongoClient(cfg["MONGO_URL"])[cfg["DB_NAME"]]


def _register_verified() -> tuple[str, requests.Session]:
    suffix = uuid.uuid4().hex[:10]
    email, pw = f"iter86_{suffix}@example.com", "Gy6#Vb3kM9zRnD2s"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": pw,
                            "name": f"iter86-{suffix}", "terms_agreed": True},
                      timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    _mongo().users.update_one(
        {"_id": ObjectId(uid)},
        {"$set": {"email_verified": True},
         "$unset": {"activation_token": "", "activation_expires_at": ""}},
    )
    s = requests.Session()
    s.post(f"{BASE_URL}/api/auth/login",
           json={"email": email, "password": pw}, timeout=TIMEOUT)
    # live test-trades sit behind the iter-122 tier gate — seed an active
    # plan so the test exercises the trade path, not billing.
    valid = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    _mongo().subscriptions.update_one(
        {"user_id": uid},
        {"$set": {"current_plan_id": "elite_ai_monthly", "valid_until": valid}},
        upsert=True)
    return uid, s


def _add_live(sess: requests.Session, broker: str = "STARTRADER") -> str:
    r = sess.post(f"{BASE_URL}/api/accounts", json={
        "label": f"iter86-{uuid.uuid4().hex[:6]}", "broker": broker,
        "server": "T-Demo",
        "account_number": "iter86-" + uuid.uuid4().hex[:8],
        "account_type": "demo", "base_currency": "USD", "mode": "live",
    }, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _seed_healthy_account(aid: str):
    """Make the account look freshly heartbeated with a tradeable symbol list
    so the test-trade endpoint doesn't refuse for unrelated reasons."""
    acc = _mongo().accounts.find_one({"_id": ObjectId(aid)})
    inst_id = f"inst_iter86_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    _mongo().accounts.update_one(
        {"_id": ObjectId(aid)},
        {"$set": {
            "status": "connected",
            "last_heartbeat": now.isoformat(),
            "ea_version": "1.55",
            "available_symbols": ["XAUUSD", "EURUSD", "BTCUSD"],
            "balance": 10000.0,
            "equity": 10000.0,
            # iter-125 identity gate: live dispatch requires a verified
            # installation identity holding the execution lease.
            "ea_identity": {"installation_id": inst_id,
                            "authoritative": True,
                            "ea_version": "1.55",
                            "verified_at": now.isoformat()},
        }},
    )
    _mongo().installations.insert_one({
        "installation_id": inst_id, "user_id": acc["user_id"],
        "account_id": aid, "terminal_path": "C:/mt5",
        "host_fingerprint": "iter86", "revoked": False, "created_at": now})
    _mongo().execution_leases.update_one(
        {"account_id": aid},
        {"$set": {"installation_id": inst_id, "user_id": acc["user_id"],
                  "revoked": False,
                  "expires_at": now + timedelta(seconds=120)}},
        upsert=True)


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


# ─────────────────── Happy path ───────────────────
def test_test_trade_happy_path(cleanup):
    uid, sess = _register_verified()
    aid = _add_live(sess)
    _seed_healthy_account(aid)
    cleanup["user_ids"].append(ObjectId(uid))
    cleanup["account_ids"].append(ObjectId(aid))

    r = sess.post(f"{BASE_URL}/api/accounts/{aid}/test-trade", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["lot_size"] == 0.01
    assert body["symbol"] in ("XAUUSD", "BTCUSD", "EURUSD")
    assert body["trade_id"]

    # Persisted trade is tagged as test
    trade = _mongo().trades.find_one({"_id": ObjectId(body["trade_id"])})
    assert trade is not None
    assert trade["is_test"] is True
    assert trade["origin"] == "test_trade"
    assert trade["lot_size"] == 0.01
    assert trade["action"] == "BUY"


# ─────────────────── Guards ───────────────────
def test_test_trade_rejects_paper_account(cleanup):
    uid, sess = _register_verified()
    cleanup["user_ids"].append(ObjectId(uid))
    paper = sess.post(f"{BASE_URL}/api/accounts", json={
        "label": "paper", "broker": "INTERNAL_PAPER", "server": "paper-virtual",
        "account_number": "PAPER1", "account_type": "demo",
        "base_currency": "USD", "mode": "paper", "initial_balance": 10000,
    }, timeout=TIMEOUT).json()
    cleanup["account_ids"].append(ObjectId(paper["id"]))

    r = sess.post(f"{BASE_URL}/api/accounts/{paper['id']}/test-trade", timeout=TIMEOUT)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "paper_account"


def test_test_trade_rejects_stale_ea(cleanup):
    uid, sess = _register_verified()
    aid = _add_live(sess)
    cleanup["user_ids"].append(ObjectId(uid))
    cleanup["account_ids"].append(ObjectId(aid))

    # 5-minute-old heartbeat
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _mongo().accounts.update_one(
        {"_id": ObjectId(aid)},
        {"$set": {"last_heartbeat": stale,
                  "available_symbols": ["XAUUSD"]}},
    )
    r = sess.post(f"{BASE_URL}/api/accounts/{aid}/test-trade", timeout=TIMEOUT)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_ea"


def test_test_trade_rejects_other_users_account(cleanup):
    uid_a, sess_a = _register_verified()
    uid_b, sess_b = _register_verified()
    aid = _add_live(sess_a)
    _seed_healthy_account(aid)
    cleanup["user_ids"] += [ObjectId(uid_a), ObjectId(uid_b)]
    cleanup["account_ids"].append(ObjectId(aid))

    r = sess_b.post(f"{BASE_URL}/api/accounts/{aid}/test-trade", timeout=TIMEOUT)
    assert r.status_code == 403


def test_test_trade_rejects_when_no_tradeable_base(cleanup):
    uid, sess = _register_verified()
    aid = _add_live(sess)
    cleanup["user_ids"].append(ObjectId(uid))
    cleanup["account_ids"].append(ObjectId(aid))

    _mongo().accounts.update_one(
        {"_id": ObjectId(aid)},
        {"$set": {
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            # MarketWatch with nothing matching XAUUSD/BTCUSD/EURUSD.
            "available_symbols": ["GER40Cash", "UK100Cash"],
        }},
    )
    r = sess.post(f"{BASE_URL}/api/accounts/{aid}/test-trade", timeout=TIMEOUT)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "no_tradeable_symbol"


def test_test_trade_404_for_unknown_account(cleanup):
    uid, sess = _register_verified()
    cleanup["user_ids"].append(ObjectId(uid))
    bogus = str(ObjectId())
    r = sess.post(f"{BASE_URL}/api/accounts/{bogus}/test-trade", timeout=TIMEOUT)
    assert r.status_code == 404


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
