"""Iter 24f — /api/trades/{id}/audit endpoint.

Returns the full deal lineage for a trade — STOIC lifecycle events (created,
break-even, partial close, closed) merged with every broker_deal that touched
the trade's mt5_ticket. Sorted chronologically.
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
from datetime import datetime, timezone

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
def me(admin_session):
    return admin_session.get(f"{API}/auth/me", timeout=10).json()


@pytest.fixture(scope="module")
def account_id(admin_session):
    accs = admin_session.get(f"{API}/accounts", timeout=10).json()
    return accs[0]["id"] if accs else None


class TestTradeAudit:
    def test_audit_unknown_trade_404(self, admin_session):
        r = admin_session.get(f"{API}/trades/000000000000000000000000/audit",
                              timeout=10)
        assert r.status_code == 404

    def test_audit_returns_stoic_lifecycle(self, admin_session, mongo_db, me, account_id):
        if not account_id: pytest.skip("no account")
        # Seed a closed trade with multiple lifecycle flags
        now = datetime.now(timezone.utc).isoformat()
        tid = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": account_id,
            "symbol": "XAUUSD", "action": "BUY", "lot_size": 0.1,
            "entry_price": 4000.0, "exit_price": 4050.0, "pnl": 50.0,
            "stop_loss": 3950, "take_profit": 4100,
            "status": "closed", "close_reason": "take_profit",
            "mt5_ticket": 70000001, "opened_at": now, "closed_at": now,
            "breakeven_set": True, "partial_closed": True, "trail_active": True,
        }).inserted_id
        try:
            r = admin_session.get(f"{API}/trades/{tid}/audit", timeout=10)
            assert r.status_code == 200
            body = r.json()
            kinds = [e["kind"] for e in body["events"]]
            assert "stoic_open" in kinds
            assert "stoic_close" in kinds
            assert "breakeven" in kinds
            assert "partial_close" in kinds
            assert "trail_active" in kinds
        finally:
            mongo_db.trades.delete_one({"_id": tid})

    def test_audit_includes_broker_deals(self, admin_session, mongo_db, me, account_id):
        if not account_id: pytest.skip("no account")
        ticket = 70000002
        # Seed trade + two broker deals (open + close)
        now = datetime.now(timezone.utc).isoformat()
        tid = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": account_id,
            "symbol": "BTCUSD", "action": "BUY", "lot_size": 0.01,
            "entry_price": 60000, "status": "open",
            "mt5_ticket": ticket, "opened_at": now,
        }).inserted_id
        mongo_db.broker_deals.insert_many([
            {"user_id": me["id"], "account_id": account_id, "mt5_ticket": ticket,
             "deal_id": 80000001, "deal_entry": "in", "symbol": "BTCUSD",
             "action": "BUY", "lots": 0.01, "price": 60000.0,
             "profit": 0, "commission": -0.5, "swap": 0,
             "deal_time": 1750000000, "magic": 901234,
             "received_at": now},
            {"user_id": me["id"], "account_id": account_id, "mt5_ticket": ticket,
             "deal_id": 80000002, "deal_entry": "out", "symbol": "BTCUSD",
             "action": "SELL", "lots": 0.01, "price": 61000.0,
             "profit": 10.0, "commission": -0.5, "swap": 0,
             "deal_time": 1750003600, "magic": 0,
             "received_at": now},
        ])
        try:
            body = admin_session.get(f"{API}/trades/{tid}/audit", timeout=10).json()
            assert body["broker_deal_count"] == 2
            broker_events = [e for e in body["events"] if e["kind"] == "broker_deal"]
            assert len(broker_events) == 2
            # Second is the MANUAL close
            close_event = next(e for e in broker_events if e["details"]["deal_entry"] == "out")
            assert close_event["details"]["is_manual"] is True
            # Sorted chronologically: deal at 1750000000 < 1750003600
            in_ev = next(e for e in broker_events if e["details"]["deal_entry"] == "in")
            assert in_ev["at"] < close_event["at"]
        finally:
            mongo_db.trades.delete_one({"_id": tid})
            mongo_db.broker_deals.delete_many({"deal_id": {"$in": [80000001, 80000002]}})

    def test_audit_other_users_trade_404(self, admin_session, mongo_db):
        # A trade owned by some other user must NOT be visible to admin
        fake_uid = "ffffffff" * 3
        tid = mongo_db.trades.insert_one({
            "user_id": fake_uid, "account_id": "xxx", "symbol": "X",
            "action": "BUY", "lot_size": 0.01, "status": "closed",
            "entry_price": 1, "mt5_ticket": 70000099,
        }).inserted_id
        try:
            r = admin_session.get(f"{API}/trades/{tid}/audit", timeout=10)
            assert r.status_code == 404
        finally:
            mongo_db.trades.delete_one({"_id": tid})

    def test_backfill_endpoint_removed(self, admin_session):
        """Verify the deprecated /backfill endpoint no longer exists —
        external-deal handles auto-sync now."""
        r = admin_session.patch(
            f"{API}/trades/000000000000000000000000/backfill",
            json={"exit_price": 1, "pnl": 1}, timeout=10,
        )
        # 404 Not Found from FastAPI when the route doesn't exist
        assert r.status_code in (404, 405)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
