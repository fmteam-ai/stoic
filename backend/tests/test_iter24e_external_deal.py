"""Iter 24e — /api/bridge/external-deal endpoint for broker-side trade events.

The MT5 EA's OnTradeTransaction handler POSTs every deal here. The endpoint
must:
  - Be idempotent on deal_id (replay returns duplicate:true, no double-apply)
  - Open an "external" trade when deal_entry='in' and no matching trade exists
  - Close + backfill exit_price/pnl when deal_entry='out' on a tracked ticket
  - Auto-create a fully-closed trade when deal_entry='out' on an unknown ticket
  - Compute realised P&L as profit + commission + swap
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import pathlib
import pytest
import requests

_FRONT_ENV = pathlib.Path(_os.path.join(_REPO_DIR, "frontend", ".env"))


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
    env_path = pathlib.Path(_os.path.join(_BACKEND_DIR, ".env"))
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("MONGO_URL="):
                _os.environ.setdefault("MONGO_URL", line.split("=", 1)[1].strip())
            if line.startswith("DB_NAME="):
                _os.environ.setdefault("DB_NAME", line.split("=", 1)[1].strip())
    client = pymongo.MongoClient(_os.environ["MONGO_URL"])
    return client[_os.environ["DB_NAME"]]


@pytest.fixture(scope="module")
def demo_account(admin_session, mongo_db):
    accs = admin_session.get(f"{API}/accounts", timeout=10).json()
    acc = next((a for a in accs if a["label"] == "demo"), None)
    if not acc:
        pytest.skip("admin has no demo account")
    from bson import ObjectId
    full = mongo_db.accounts.find_one({"_id": ObjectId(acc["id"])})
    return full


def _cleanup(mongo_db, *, ticket=None, deal_ids=()):
    if ticket is not None:
        mongo_db.trades.delete_many({"mt5_ticket": ticket})
    if deal_ids:
        mongo_db.broker_deals.delete_many({"deal_id": {"$in": list(deal_ids)}})


class TestExternalDeal:
    BASE_TICKET = 77000000

    def test_invalid_bridge_token_401(self):
        r = requests.post(f"{API}/bridge/external-deal",
                          json={"bridge_token": "garbage", "mt5_ticket": 1,
                                "deal_id": 1, "deal_entry": "in",
                                "symbol": "X", "action": "BUY", "lots": 0.1,
                                "price": 1.0}, timeout=10)
        assert r.status_code == 401

    def test_open_creates_external_trade(self, demo_account, mongo_db):
        ticket = self.BASE_TICKET + 1
        deal_id = 91000000 + 1
        try:
            r = requests.post(f"{API}/bridge/external-deal", json={
                "bridge_token": demo_account["bridge_token"],
                "mt5_ticket": ticket, "deal_id": deal_id,
                "deal_entry": "in", "symbol": "BTCUSD", "action": "BUY",
                "lots": 0.01, "price": 62000.0, "magic": 0,
            }, timeout=10)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["external_open"] is True
            trade = mongo_db.trades.find_one({"mt5_ticket": ticket})
            assert trade["status"] == "open"
            assert trade["origin"] == "external"
            assert trade["external_open"] is True
        finally:
            _cleanup(mongo_db, ticket=ticket, deal_ids=[deal_id])

    def test_close_backfills_existing_trade(self, demo_account, mongo_db):
        """Sim PANIC orphan: trade closed in STOIC with no exit. External
        deal close must backfill exit_price + pnl."""
        ticket = self.BASE_TICKET + 2
        deal_id = 91000000 + 2
        try:
            mongo_db.trades.insert_one({
                "user_id": demo_account["user_id"],
                "account_id": str(demo_account["_id"]),
                "symbol": "XAUUSD", "action": "SELL", "lot_size": 1.0,
                "entry_price": 4100.0, "stop_loss": 4150, "take_profit": 4000,
                "status": "closed", "exit_price": None, "pnl": 0,
                "mt5_ticket": ticket, "close_reason": "panic",
            })
            r = requests.post(f"{API}/bridge/external-deal", json={
                "bridge_token": demo_account["bridge_token"],
                "mt5_ticket": ticket, "deal_id": deal_id,
                "deal_entry": "out", "symbol": "XAUUSD", "action": "BUY",
                "lots": 1.0, "price": 4080.0,
                "profit": 20.0, "commission": -1.5, "swap": -0.5, "magic": 0,
            }, timeout=10)
            assert r.status_code == 200, r.text
            assert r.json()["pnl"] == pytest.approx(18.0)   # 20 - 1.5 - 0.5
            doc = mongo_db.trades.find_one({"mt5_ticket": ticket})
            assert doc["exit_price"] == 4080.0
            assert doc["pnl"] == pytest.approx(18.0)
            assert doc["backfilled"] is True
            assert "external_close" in doc["close_reason"]
        finally:
            _cleanup(mongo_db, ticket=ticket, deal_ids=[deal_id])

    def test_idempotency_replays_are_safe(self, demo_account, mongo_db):
        ticket = self.BASE_TICKET + 3
        deal_id = 91000000 + 3
        try:
            payload = {
                "bridge_token": demo_account["bridge_token"],
                "mt5_ticket": ticket, "deal_id": deal_id,
                "deal_entry": "in", "symbol": "BTCUSD", "action": "BUY",
                "lots": 0.05, "price": 62500.0, "magic": 0,
            }
            r1 = requests.post(f"{API}/bridge/external-deal", json=payload, timeout=10)
            assert r1.status_code == 200
            assert r1.json().get("created")
            r2 = requests.post(f"{API}/bridge/external-deal", json=payload, timeout=10)
            assert r2.status_code == 200
            assert r2.json().get("duplicate") is True
            count = mongo_db.trades.count_documents({"mt5_ticket": ticket})
            assert count == 1   # not double-inserted
        finally:
            _cleanup(mongo_db, ticket=ticket, deal_ids=[deal_id])

    def test_close_without_prior_trade_creates_audit_row(self, demo_account, mongo_db):
        """Open + close happened entirely on MT5 without STOIC ever knowing.
        The close event should still land as a fully-closed audit trade so
        the user's stats reflect it."""
        ticket = self.BASE_TICKET + 4
        deal_id = 91000000 + 4
        try:
            r = requests.post(f"{API}/bridge/external-deal", json={
                "bridge_token": demo_account["bridge_token"],
                "mt5_ticket": ticket, "deal_id": deal_id,
                "deal_entry": "out", "symbol": "EURUSD", "action": "SELL",
                "lots": 0.1, "price": 1.0859,
                "profit": 50.0, "commission": -1.0, "swap": 0.0, "magic": 0,
            }, timeout=10)
            assert r.status_code == 200, r.text
            doc = mongo_db.trades.find_one({"mt5_ticket": ticket})
            assert doc is not None
            assert doc["status"] == "closed"
            assert doc["exit_price"] == 1.0859
            assert doc["pnl"] == pytest.approx(49.0)
            assert doc["external_open"] is True
            assert doc["external_close"] is True
        finally:
            _cleanup(mongo_db, ticket=ticket, deal_ids=[deal_id])

    def test_open_skipped_when_ticket_already_tracked(self, demo_account, mongo_db):
        ticket = self.BASE_TICKET + 5
        deal_id = 91000000 + 5
        try:
            mongo_db.trades.insert_one({
                "user_id": demo_account["user_id"],
                "account_id": str(demo_account["_id"]),
                "symbol": "XAUUSD", "action": "BUY", "lot_size": 1.0,
                "entry_price": 4050.0, "status": "open",
                "mt5_ticket": ticket,
            })
            r = requests.post(f"{API}/bridge/external-deal", json={
                "bridge_token": demo_account["bridge_token"],
                "mt5_ticket": ticket, "deal_id": deal_id,
                "deal_entry": "in", "symbol": "XAUUSD", "action": "BUY",
                "lots": 1.0, "price": 4051.0, "magic": 901234,  # our EA magic
            }, timeout=10)
            assert r.status_code == 200
            assert r.json().get("noop") == "ticket_already_tracked"
            assert mongo_db.trades.count_documents({"mt5_ticket": ticket}) == 1
        finally:
            _cleanup(mongo_db, ticket=ticket, deal_ids=[deal_id])
