"""Iter 25k — Partial-close discrimination + open_tickets derivation.

Two related bugs caused MT5↔STOIC desync:

1. /external-deal treated every `deal_entry=out` as a FULL close even when
   the deal's lots was smaller than the position's lot_size (a partial
   close). STOIC closed the trade while the broker kept the remainder open.

2. EA v1.25+ stopped sending `open_tickets` as a top-level field — it now
   sends the full `positions` array. The DB's `open_tickets` got stuck at
   the last legacy-EA value forever, causing the reconciler to mis-target.
   Fix: derive `open_tickets` from `positions[*].ticket` when the field
   isn't sent explicitly.
"""
import os
import pathlib
import pytest
import requests
from datetime import datetime, timezone
from bson import ObjectId


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


@pytest.fixture
def fresh_account(mongo_db, me):
    acc = {
        "user_id": me["id"], "label": "TEST_iter25k",
        "broker": "RoboForex", "account_type": "demo",
        "account_number": "TESTK",
        "bridge_token": "iter25k-" + str(ObjectId()),
        "status": "connected", "balance": 1000, "equity": 1000,
        "mode": "live", "created_at": datetime.now(timezone.utc).isoformat(),
    }
    res = mongo_db.accounts.insert_one(acc)
    acc["_id"] = res.inserted_id
    yield acc
    mongo_db.accounts.delete_one({"_id": acc["_id"]})
    mongo_db.trades.delete_many({"account_id": str(acc["_id"])})
    mongo_db.broker_deals.delete_many({"account_id": str(acc["_id"])})


class TestPartialClosePreservesTrade:
    def test_partial_out_deal_keeps_trade_open_and_reduces_lot(
        self, mongo_db, fresh_account, me
    ):
        """Out-deal with lots < position lot_size must be treated as a partial
        close: trade stays open, lot_size shrinks, P&L accumulates."""
        now = datetime.now(timezone.utc).isoformat()
        tid = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": str(fresh_account["_id"]),
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.20,
            "entry_price": 4050.0, "stop_loss": 4070.0,
            "status": "open", "mt5_ticket": 700020001,
            "opened_at": now, "pnl": 0.0,
        }).inserted_id

        r = requests.post(f"{API}/bridge/external-deal", json={
            "bridge_token": fresh_account["bridge_token"],
            "mt5_ticket": 700020001, "deal_id": 800020001,
            "deal_entry": "out", "symbol": "XAUUSD", "action": "SELL",
            "lots": 0.14, "price": 4030.0, "profit": 280.0,
            "deal_time": 1719500000, "magic": 0,
        }, timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("partial_close") is True
        assert body.get("new_lot_size") == 0.06

        doc = mongo_db.trades.find_one({"_id": tid})
        assert doc["status"] == "open"
        assert doc["lot_size"] == 0.06
        assert doc.get("partial_closed") is True
        assert doc.get("pnl") == 280.0
        assert doc.get("exit_price") is None

    def test_full_out_deal_still_closes_trade(
        self, mongo_db, fresh_account, me
    ):
        """When deal lots == position lots, close the trade fully (legacy path)."""
        now = datetime.now(timezone.utc).isoformat()
        tid = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": str(fresh_account["_id"]),
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.10,
            "entry_price": 4050.0, "stop_loss": 4070.0,
            "status": "open", "mt5_ticket": 700020002,
            "opened_at": now,
        }).inserted_id

        r = requests.post(f"{API}/bridge/external-deal", json={
            "bridge_token": fresh_account["bridge_token"],
            "mt5_ticket": 700020002, "deal_id": 800020002,
            "deal_entry": "out", "symbol": "XAUUSD", "action": "SELL",
            "lots": 0.10, "price": 4025.0, "profit": 250.0,
            "deal_time": 1719500100, "magic": 0,
        }, timeout=10)
        assert r.status_code == 200
        assert r.json().get("partial_close") is not True

        doc = mongo_db.trades.find_one({"_id": tid})
        assert doc["status"] == "closed"
        assert doc.get("exit_price") == 4025.0

    def test_partial_close_with_1pct_tolerance_still_closes_fully(
        self, mongo_db, fresh_account, me
    ):
        """Float rounding by broker (e.g. 0.099 instead of 0.10) must still
        be treated as a full close — tolerance window is 1%."""
        now = datetime.now(timezone.utc).isoformat()
        tid = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": str(fresh_account["_id"]),
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.10,
            "entry_price": 4050.0, "stop_loss": 4070.0,
            "status": "open", "mt5_ticket": 700020003,
            "opened_at": now,
        }).inserted_id

        r = requests.post(f"{API}/bridge/external-deal", json={
            "bridge_token": fresh_account["bridge_token"],
            "mt5_ticket": 700020003, "deal_id": 800020003,
            "deal_entry": "out", "symbol": "XAUUSD", "action": "SELL",
            "lots": 0.0995, "price": 4025.0, "profit": 250.0,
            "deal_time": 1719500200, "magic": 0,
        }, timeout=10)
        assert r.status_code == 200
        assert mongo_db.trades.find_one({"_id": tid})["status"] == "closed"


class TestOpenTicketsDerivedFromPositions:
    def test_heartbeat_with_positions_but_no_open_tickets_field(
        self, mongo_db, fresh_account
    ):
        """v1.25+ EAs send `positions` array but not `open_tickets`.
        Backend must derive tickets from positions so reconciler works."""
        r = requests.post(f"{API}/bridge/heartbeat", json={
            "bridge_token": fresh_account["bridge_token"],
            "balance": 1000, "equity": 1000,
            "open_positions": 2,
            "client_version": "1.26",
            "positions": [
                {"ticket": 700030001, "symbol": "XAUUSD", "type": "SELL",
                 "volume": 0.1, "price_open": 4050.0, "sl": 4070.0, "tp": 4020.0,
                 "time_open": 1719500300},
                {"ticket": 700030002, "symbol": "XAUUSD", "type": "SELL",
                 "volume": 0.05, "price_open": 4055.0, "sl": 4075.0, "tp": 4025.0,
                 "time_open": 1719500400},
            ],
        }, timeout=10)
        assert r.status_code == 200, r.text

        doc = mongo_db.accounts.find_one({"_id": fresh_account["_id"]})
        assert set(doc["open_tickets"]) == {700030001, 700030002}
