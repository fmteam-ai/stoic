"""iter-90 · Heartbeat reconcile must trigger when EA reports zero positions.

The original bridge_routes.py used `if payload.open_tickets is None and
payload.positions:` — which silently skipped reconciliation when the EA sent
an *explicitly-empty* positions array. Symptom: trades stayed `status=open`
in the DB forever after the broker closed them (SL/TP hit), because the next
heartbeat with `positions=[]` was treated as "no info" instead of "zero open".

Also covers the 45s grace window in trade_reconciler — freshly-opened trades
must not be reaped during the EA's confirmation round-trip.
"""
from __future__ import annotations
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta

import requests
from bson import ObjectId
from pymongo import MongoClient
from live_target import require_live_base_url

# Load /app/backend/.env so trade_reconciler can be imported directly
# (it reads MONGO_URL / DB_NAME at module load time via database.get_db()).
_BACKEND_DIR = _BACKEND_DIR
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
with open(f"{_BACKEND_DIR}/.env") as _f:
    for _ln in _f:
        if "=" in _ln and not _ln.lstrip().startswith("#"):
            _k, _v = _ln.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

BASE_URL = require_live_base_url()
TIMEOUT = 30


def _mongo():
    with open(_os.path.join(_BACKEND_DIR, ".env")) as f:
        cfg = {ln.split("=", 1)[0]: ln.split("=", 1)[1].strip().strip("\"'")
               for ln in f if "=" in ln}
    return MongoClient(cfg["MONGO_URL"])[cfg["DB_NAME"]]


def _register_verified() -> tuple[str, requests.Session]:
    suffix = uuid.uuid4().hex[:10]
    email, pw = f"iter90_{suffix}@example.com", "Gy6#Vb3kM9zRnD2s"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": pw,
                            "name": f"iter90-{suffix}", "terms_agreed": True},
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


def test_reconcile_account_closes_orphan_when_broker_reports_zero():
    """The original iter-90 bug: trade open in DB, broker reports empty
    positions list, reconcile must close the orphan."""
    import asyncio
    from trade_reconciler import reconcile_account

    uid, sess = _register_verified()
    db = _mongo()

    # Create an account
    r = sess.post(f"{BASE_URL}/api/accounts", json={
        "label": f"iter90-{uuid.uuid4().hex[:6]}",
        "broker": "STARTRADER", "server": "T-Demo",
        "account_number": f"iter90-{uuid.uuid4().hex[:8]}",
        "account_type": "demo", "base_currency": "USD", "mode": "live",
    }, timeout=TIMEOUT)
    aid = r.json()["id"]

    # Insert an OPEN trade with a real-looking ticket, opened 5 minutes ago
    # (past the 45s grace window).
    old_iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    trade_id = ObjectId()
    db.trades.insert_one({
        "_id": trade_id,
        "user_id": uid,
        "account_id": aid,
        "symbol": "XAUUSD", "action": "BUY", "lot_size": 0.01,
        "status": "open", "mt5_ticket": 999_888_777,
        "opened_at": old_iso, "entry_price": 2400.0,
    })

    try:
        from conftest import run_async
        result = run_async(
            reconcile_account(aid, open_tickets=[], source="test_iter90")
        )
        # Should have closed exactly 1 orphan
        assert result["closed_count"] == 1, result
        doc = db.trades.find_one({"_id": trade_id})
        assert doc["status"] == "closed"
        assert "broker_reconciled" in doc["close_reason"]
        assert doc.get("reconciled") is True
    finally:
        db.trades.delete_one({"_id": trade_id})
        db.accounts.delete_one({"_id": ObjectId(aid)})
        db.users.delete_one({"_id": ObjectId(uid)})


def test_reconcile_account_skips_freshly_opened_trade_within_grace_window():
    """45s grace window: a trade opened 2s ago must NOT be reaped even if
    the broker hasn't included it in positions yet (EA confirmation in-flight)."""
    import asyncio
    from trade_reconciler import reconcile_account

    uid, sess = _register_verified()
    db = _mongo()
    r = sess.post(f"{BASE_URL}/api/accounts", json={
        "label": f"iter90-grace-{uuid.uuid4().hex[:6]}",
        "broker": "STARTRADER", "server": "T-Demo",
        "account_number": f"iter90g-{uuid.uuid4().hex[:8]}",
        "account_type": "demo", "base_currency": "USD", "mode": "live",
    }, timeout=TIMEOUT)
    aid = r.json()["id"]

    fresh_iso = datetime.now(timezone.utc).isoformat()  # just now
    trade_id = ObjectId()
    db.trades.insert_one({
        "_id": trade_id,
        "user_id": uid,
        "account_id": aid,
        "symbol": "XAUUSD", "action": "BUY", "lot_size": 0.01,
        "status": "open", "mt5_ticket": 111_222_333,
        "opened_at": fresh_iso, "entry_price": 2400.0,
    })

    try:
        from conftest import run_async
        result = run_async(
            reconcile_account(aid, open_tickets=[], source="test_iter90_grace")
        )
        assert result["closed_count"] == 0, \
            f"freshly-opened trade was reaped within grace window: {result}"
        doc = db.trades.find_one({"_id": trade_id})
        assert doc["status"] == "open", "trade should still be open"
    finally:
        db.trades.delete_one({"_id": trade_id})
        db.accounts.delete_one({"_id": ObjectId(aid)})
        db.users.delete_one({"_id": ObjectId(uid)})


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
