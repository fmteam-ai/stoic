"""Iter 24d — Manual P&L backfill for orphaned closed trades.

The use case: PANIC marks trades close_requested→closed before the broker
confirms. The user later closes them manually on MT5 with a real profit, but
the EA's manual-close detection is incomplete and exit_price/pnl never land.
This endpoint lets the user paste the values from MT5 history.

Contracts:
  - PATCH /api/trades/{id}/backfill body={exit_price, pnl}
  - Only allowed on closed trades with exit_price == null
  - Refuses on open/pending/cancelled/failed
  - Refuses if exit_price already exists (no silent override)
  - Sets backfilled=True + backfilled_at + appends '+backfill' to close_reason
"""
import os
import pathlib
import pytest
import requests
from datetime import datetime, timezone

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


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=15)
    assert r.status_code == 200
    return s


def _seed_orphan_trade(db, user_id, account_id, *, exit_price=None, status="closed"):
    """Insert a closed-with-no-exit trade for backfill testing.

    Returns the inserted _id (as str).
    """
    doc = {
        "user_id": user_id,
        "account_id": account_id,
        "symbol": "XAUUSD",
        "action": "SELL",
        "lot_size": 1.0,
        "entry_price": 4100.0,
        "stop_loss": 4150.0,
        "take_profit": 4000.0,
        "exit_price": exit_price,
        "pnl": 0,
        "status": status,
        "mt5_ticket": 99988877,
        "close_reason": "panic",
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }
    res = db.trades.insert_one(doc)
    return str(res.inserted_id)


@pytest.fixture(scope="module")
def mongo_db():
    """Sync pymongo handle for direct seeding."""
    import os as _os
    import pymongo
    env_path = pathlib.Path("/app/backend/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("MONGO_URL="):
                _os.environ.setdefault("MONGO_URL", line.split("=", 1)[1].strip())
            if line.startswith("DB_NAME="):
                _os.environ.setdefault("DB_NAME", line.split("=", 1)[1].strip())
    url = _os.environ["MONGO_URL"]
    name = _os.environ["DB_NAME"]
    client = pymongo.MongoClient(url)
    return client[name]


@pytest.fixture(scope="module")
def me(admin_session):
    return admin_session.get(f"{API}/auth/me", timeout=10).json()


@pytest.fixture(scope="module")
def account_id(admin_session):
    accs = admin_session.get(f"{API}/accounts", timeout=10).json()
    return accs[0]["id"] if accs else None


class TestBackfillTrade:
    def test_backfill_success(self, admin_session, mongo_db, me, account_id):
        if not account_id:
            pytest.skip("admin has no accounts")
        tid = _seed_orphan_trade(mongo_db, me["id"], account_id, exit_price=None)
        try:
            r = admin_session.patch(f"{API}/trades/{tid}/backfill",
                                    json={"exit_price": 4080.5, "pnl": 19.5},
                                    timeout=10)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["exit_price"] == 4080.5
            assert body["pnl"] == 19.5

            # Verify persisted
            from bson import ObjectId
            doc = mongo_db.trades.find_one({"_id": ObjectId(tid)})
            assert doc["exit_price"] == 4080.5
            assert doc["pnl"] == 19.5
            assert doc["backfilled"] is True
            assert "+backfill" in doc["close_reason"]
        finally:
            from bson import ObjectId
            mongo_db.trades.delete_one({"_id": ObjectId(tid)})

    def test_backfill_refuses_when_exit_already_set(self, admin_session, mongo_db, me, account_id):
        if not account_id:
            pytest.skip("admin has no accounts")
        tid = _seed_orphan_trade(mongo_db, me["id"], account_id, exit_price=4100.0)
        try:
            r = admin_session.patch(f"{API}/trades/{tid}/backfill",
                                    json={"exit_price": 4080.0, "pnl": 20}, timeout=10)
            assert r.status_code == 400
            assert "exit price" in r.text.lower()
        finally:
            from bson import ObjectId
            mongo_db.trades.delete_one({"_id": ObjectId(tid)})

    def test_backfill_refuses_on_open_trade(self, admin_session, mongo_db, me, account_id):
        if not account_id:
            pytest.skip("admin has no accounts")
        tid = _seed_orphan_trade(mongo_db, me["id"], account_id, status="open")
        try:
            r = admin_session.patch(f"{API}/trades/{tid}/backfill",
                                    json={"exit_price": 4080, "pnl": 10}, timeout=10)
            assert r.status_code == 400
            assert "closed" in r.text.lower()
        finally:
            from bson import ObjectId
            mongo_db.trades.delete_one({"_id": ObjectId(tid)})

    def test_backfill_supports_negative_pnl(self, admin_session, mongo_db, me, account_id):
        if not account_id:
            pytest.skip("admin has no accounts")
        tid = _seed_orphan_trade(mongo_db, me["id"], account_id)
        try:
            r = admin_session.patch(f"{API}/trades/{tid}/backfill",
                                    json={"exit_price": 4120, "pnl": -25.5}, timeout=10)
            assert r.status_code == 200
            assert r.json()["pnl"] == -25.5
        finally:
            from bson import ObjectId
            mongo_db.trades.delete_one({"_id": ObjectId(tid)})

    def test_backfill_unknown_trade_404(self, admin_session):
        r = admin_session.patch(f"{API}/trades/000000000000000000000000/backfill",
                                json={"exit_price": 1, "pnl": 1}, timeout=10)
        assert r.status_code == 404
