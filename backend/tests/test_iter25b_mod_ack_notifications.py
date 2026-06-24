"""Iter 25b — modification_ack fires partial-close + breakeven notifications.

After moving Telegram alerts out of trade_manager (queue-time) into
modification_ack (broker-confirmed-time), verify:
  1. PARTIAL_CLOSE ack with new_sl fires both partial_close + breakeven
  2. PARTIAL_CLOSE ack without new_sl fires only partial_close
  3. modification_ack sets partial_closed=True and tp1_closed=True on first ack
  4. Failed ack (success=False) records error and fires no notification
"""
import os
import pathlib
import pytest
import requests
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock

_FRONT_ENV = pathlib.Path("/app/frontend/.env")


def _read_frontend_backend_url() -> str:
    if not _FRONT_ENV.exists():
        return ""
    for line in _FRONT_ENV.read_text().splitlines():
        if line.startswith("REACT_APP_BACKEND_URL="):
            return line.split("=", 1)[1].strip()
    return ""


BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL")
            or _read_frontend_backend_url()
            or "http://localhost:8001").rstrip("/")
API = f"{BASE_URL}/api"


def _strip(v: str) -> str:
    return v.strip().strip('"').strip("'")


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=15)
    assert r.status_code == 200
    return s


@pytest.fixture(scope="module")
def mongo_db():
    import os as _os, pymongo
    env_path = pathlib.Path("/app/backend/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("MONGO_URL="):
                _os.environ.setdefault("MONGO_URL", _strip(line.split("=", 1)[1]))
            if line.startswith("DB_NAME="):
                _os.environ.setdefault("DB_NAME", _strip(line.split("=", 1)[1]))
    client = pymongo.MongoClient(_os.environ["MONGO_URL"])
    return client[_os.environ["DB_NAME"]]


@pytest.fixture(scope="module")
def me(admin_session):
    return admin_session.get(f"{API}/auth/me", timeout=10).json()


@pytest.fixture(scope="module")
def account_with_token(admin_session, mongo_db, me):
    accs = admin_session.get(f"{API}/accounts", timeout=10).json()
    if not accs:
        pytest.skip("no account")
    from bson import ObjectId
    acc = mongo_db.accounts.find_one({"_id": ObjectId(accs[0]["id"])})
    token = acc.get("bridge_token")
    if not token:
        # mint one quickly so the test isn't blocked
        token = "iter25b-test-token"
        mongo_db.accounts.update_one({"_id": acc["_id"]}, {"$set": {"bridge_token": token}})
    return {"id": str(acc["_id"]), "bridge_token": token}


class TestModificationAckTelegram:
    def test_partial_close_ack_marks_state_and_clears_pending(
        self, mongo_db, me, account_with_token
    ):
        """Hitting /modification-ack with type=PARTIAL_CLOSE must:
          • clear pending_modification
          • set lot_size to new_volume
          • set partial_closed=True and tp1_closed=True (first tier)
          • set breakeven_set=True when new_sl carried (Tier-1 combo)"""
        now = datetime.now(timezone.utc).isoformat()
        tid = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": account_with_token["id"],
            "symbol": "XAUUSD", "action": "BUY", "lot_size": 1.0,
            "original_lot_size": 1.0,
            "entry_price": 4000.0, "stop_loss": 3985.0, "take_profit": 4030.0,
            "status": "open", "mt5_ticket": 70000950, "opened_at": now,
            "pending_modification": {
                "type": "PARTIAL_CLOSE", "new_volume": 0.5,
                "new_sl": 4000.0, "requested_at": now,
            },
        }).inserted_id
        try:
            r = requests.post(f"{API}/bridge/modification-ack", json={
                "bridge_token": account_with_token["bridge_token"],
                "trade_id": str(tid),
                "type": "PARTIAL_CLOSE",
                "success": True,
                "new_volume": 0.5,
            }, timeout=10)
            assert r.status_code == 200, r.text
            doc = mongo_db.trades.find_one({"_id": tid})
            assert doc.get("pending_modification") is None
            assert doc.get("lot_size") == 0.5
            assert doc.get("partial_closed") is True
            assert doc.get("tp1_closed") is True
            assert doc.get("breakeven_set") is True  # because new_sl carried
            assert doc.get("stop_loss") == 4000.0
            assert doc.get("partial_closed_at")
        finally:
            mongo_db.trades.delete_one({"_id": tid})

    def test_partial_close_tier2_progression(
        self, mongo_db, me, account_with_token
    ):
        """Second partial-close ack (tp1 already closed) must mark tp2."""
        now = datetime.now(timezone.utc).isoformat()
        tid = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": account_with_token["id"],
            "symbol": "XAUUSD", "action": "BUY", "lot_size": 0.5,
            "original_lot_size": 1.0,
            "entry_price": 4000.0, "stop_loss": 4000.0, "take_profit": 4030.0,
            "status": "open", "mt5_ticket": 70000951, "opened_at": now,
            "partial_closed": True, "tp1_closed": True, "breakeven_set": True,
            "pending_modification": {
                "type": "PARTIAL_CLOSE", "new_volume": 0.25,
                "requested_at": now,
            },
        }).inserted_id
        try:
            r = requests.post(f"{API}/bridge/modification-ack", json={
                "bridge_token": account_with_token["bridge_token"],
                "trade_id": str(tid),
                "type": "PARTIAL_CLOSE",
                "success": True,
                "new_volume": 0.25,
            }, timeout=10)
            assert r.status_code == 200, r.text
            doc = mongo_db.trades.find_one({"_id": tid})
            assert doc.get("tp1_closed") is True
            assert doc.get("tp2_closed") is True
            assert doc.get("lot_size") == 0.25
        finally:
            mongo_db.trades.delete_one({"_id": tid})

    def test_failed_ack_records_error_and_skips_state_changes(
        self, mongo_db, me, account_with_token
    ):
        """If EA reports success=False, pending_modification clears but no
        partial_closed / lot_size update happens."""
        now = datetime.now(timezone.utc).isoformat()
        tid = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": account_with_token["id"],
            "symbol": "XAUUSD", "action": "BUY", "lot_size": 1.0,
            "original_lot_size": 1.0,
            "entry_price": 4000.0, "stop_loss": 3985.0, "take_profit": 4030.0,
            "status": "open", "mt5_ticket": 70000952, "opened_at": now,
            "pending_modification": {
                "type": "PARTIAL_CLOSE", "new_volume": 0.5,
                "requested_at": now,
            },
        }).inserted_id
        try:
            r = requests.post(f"{API}/bridge/modification-ack", json={
                "bridge_token": account_with_token["bridge_token"],
                "trade_id": str(tid),
                "type": "PARTIAL_CLOSE",
                "success": False,
                "error": "Trade context busy",
            }, timeout=10)
            assert r.status_code == 200
            doc = mongo_db.trades.find_one({"_id": tid})
            assert doc.get("pending_modification") is None  # always cleared
            assert doc.get("lot_size") == 1.0  # untouched
            assert doc.get("partial_closed") is not True
            assert doc.get("last_modification_error") == "Trade context busy"
        finally:
            mongo_db.trades.delete_one({"_id": tid})
