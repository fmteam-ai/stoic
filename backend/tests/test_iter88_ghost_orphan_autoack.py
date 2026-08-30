"""iter-88 · Ghost trades attached to deleted accounts auto-ack.

A common cause of stuck "X closed trade(s) missing exit price" warnings is
orphan trade rows left behind after an account is deleted (testing-agent
synthetics, user-deleted brokers). The EA history sweep can never backfill
exit_price on these because the account itself is gone.

Bot health (/api/bot/health-score) must auto-acknowledge them with
reason="account_deleted" so the warning stops surfacing.
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
from datetime import datetime, timezone

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
    email, pw = f"iter88_{suffix}@example.com", "Gy6#Vb3kM9zRnD2s"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": pw,
                            "name": f"iter88-{suffix}", "terms_agreed": True},
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
    return uid, s


def test_health_score_auto_acks_orphan_ghost():
    uid, sess = _register_verified()
    db = _mongo()

    # Insert a synthetic closed-but-no-exit trade tied to a non-existent
    # account, closed *recently* so the 24h rule wouldn't catch it.
    fake_account_id = str(ObjectId())  # never inserted into accounts
    trade_id = ObjectId()
    db.trades.insert_one({
        "_id": trade_id,
        "user_id": uid,
        "account_id": fake_account_id,
        "symbol": "XAUUSD",
        "action": "BUY",
        "lot_size": 0.01,
        "status": "closed",
        "exit_price": None,
        "ghost_acknowledged": False,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "closed_at": datetime.now(timezone.utc).isoformat(),  # fresh — within 24h
        "origin": "manual",
    })

    try:
        # Hit health-score — it should auto-ack the orphan and report 0 ghosts.
        r = sess.get(f"{BASE_URL}/api/bot/health-score", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        ghost_issues = [i for i in body.get("issues", []) if i.get("code") == "ghost_trades"]
        assert ghost_issues == [], f"orphan should have been auto-acked, got: {ghost_issues}"

        # And the trade row should be tagged with the new reason.
        doc = db.trades.find_one({"_id": trade_id})
        assert doc["ghost_acknowledged"] is True
        assert doc["ghost_auto_ack_reason"] == "account_deleted"
    finally:
        db.trades.delete_one({"_id": trade_id})
        db.users.delete_one({"_id": ObjectId(uid)})


def test_health_score_does_not_ack_live_account_ghost():
    """Sanity: ghosts on a STILL-LIVING account should NOT be auto-acked as
    account_deleted — they're recoverable by the EA history sweep."""
    uid, sess = _register_verified()
    db = _mongo()

    # Create a real account
    r = sess.post(f"{BASE_URL}/api/accounts", json={
        "label": f"iter88-live-{uuid.uuid4().hex[:6]}",
        "broker": "STARTRADER", "server": "T-Demo",
        "account_number": f"iter88-{uuid.uuid4().hex[:8]}",
        "account_type": "demo", "base_currency": "USD", "mode": "live",
    }, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    real_account_id = r.json()["id"]

    trade_id = ObjectId()
    db.trades.insert_one({
        "_id": trade_id,
        "user_id": uid,
        "account_id": real_account_id,
        "symbol": "XAUUSD", "action": "BUY", "lot_size": 0.01,
        "status": "closed", "exit_price": None,
        "ghost_acknowledged": False,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "origin": "manual",
    })
    try:
        r = sess.get(f"{BASE_URL}/api/bot/health-score", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        doc = db.trades.find_one({"_id": trade_id})
        # Either NOT acked, or acked for a different reason — definitely
        # not account_deleted because the account is alive.
        assert doc.get("ghost_auto_ack_reason") != "account_deleted"
    finally:
        db.trades.delete_one({"_id": trade_id})
        db.accounts.delete_one({"_id": ObjectId(real_account_id)})
        db.users.delete_one({"_id": ObjectId(uid)})


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
