"""Iter 25e — Backfill exit endpoint + Position Sizing Preview.

backfill-exit: lets the user manually attach exit_price + pnl to a ghost
closed trade (reconciler-closed with no broker deal report). The classic
"closed on a different MT5 terminal" case.

sizing-preview: returns the effective lot the bot would open at confidences
55-90% given the user's actual account + risk profile + max_lot_size. Drives
the live preview panel on the BotConfig page.
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
def account_id(admin_session):
    accs = admin_session.get(f"{API}/accounts", timeout=10).json()
    return accs[0]["id"] if accs else None


class TestBackfillExit:
    def test_backfill_succeeds_on_ghost_trade(
        self, admin_session, mongo_db, me, account_id
    ):
        """Trade closed=true + exit_price=None → backfill must write exit_price,
        pnl, mark backfilled_exit=true and tag close_reason."""
        if not account_id:
            pytest.skip("no account")
        now = datetime.now(timezone.utc).isoformat()
        tid = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": account_id,
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.5,
            "entry_price": 4080.0, "stop_loss": 4095.0, "take_profit": 4050.0,
            "status": "closed", "exit_price": None, "pnl": 0.0,
            "close_reason": "manual", "mt5_ticket": 70000970,
            "opened_at": now, "closed_at": now,
        }).inserted_id
        try:
            r = admin_session.post(
                f"{API}/trades/{tid}/backfill-exit",
                json={"exit_price": 4055.50, "pnl": 1023.55},
                timeout=10,
            )
            assert r.status_code == 200, r.text
            doc = mongo_db.trades.find_one({"_id": tid})
            assert doc["exit_price"] == 4055.50
            assert doc["pnl"] == 1023.55
            assert doc["backfilled_exit"] is True
            assert doc["backfilled_exit_at"]
            assert "user_backfill" in (doc.get("close_reason") or "")
        finally:
            mongo_db.trades.delete_one({"_id": tid})

    def test_backfill_refuses_when_exit_price_already_set(
        self, admin_session, mongo_db, me, account_id
    ):
        if not account_id:
            pytest.skip("no account")
        now = datetime.now(timezone.utc).isoformat()
        tid = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": account_id,
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.5,
            "entry_price": 4080.0, "stop_loss": 4095.0,
            "status": "closed", "exit_price": 4060.0, "pnl": 100.0,
            "mt5_ticket": 70000971, "opened_at": now, "closed_at": now,
        }).inserted_id
        try:
            r = admin_session.post(
                f"{API}/trades/{tid}/backfill-exit",
                json={"exit_price": 4055.50, "pnl": 1023.55},
                timeout=10,
            )
            assert r.status_code == 400
            assert "exit_price" in r.text.lower()
        finally:
            mongo_db.trades.delete_one({"_id": tid})

    def test_backfill_refuses_on_non_closed_trade(
        self, admin_session, mongo_db, me, account_id
    ):
        if not account_id:
            pytest.skip("no account")
        now = datetime.now(timezone.utc).isoformat()
        tid = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": account_id,
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.5,
            "entry_price": 4080.0, "stop_loss": 4095.0,
            "status": "open", "exit_price": None,
            "mt5_ticket": 70000972, "opened_at": now,
        }).inserted_id
        try:
            r = admin_session.post(
                f"{API}/trades/{tid}/backfill-exit",
                json={"exit_price": 4055.50, "pnl": 1023.55},
                timeout=10,
            )
            assert r.status_code == 400
        finally:
            mongo_db.trades.delete_one({"_id": tid})

    def test_backfill_404_on_other_user_trade(self, admin_session):
        r = admin_session.post(
            f"{API}/trades/000000000000000000000000/backfill-exit",
            json={"exit_price": 4055.50, "pnl": 100.0},
            timeout=10,
        )
        assert r.status_code == 404


class TestSizingPreview:
    def test_preview_returns_scaled_rows(self, admin_session):
        r = admin_session.get(f"{API}/bot/sizing-preview?symbol=XAUUSD", timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        # Must include the standard confidence ladder
        confs = [row["confidence_pct"] for row in body["rows"]]
        assert confs == [55, 60, 65, 70, 75, 80, 85, 90]
        # Effective lots must be monotonic non-decreasing
        lots = [row["effective_lot"] for row in body["rows"]]
        for prev, curr in zip(lots, lots[1:]):
            assert curr >= prev, lots
        # Sanity on context fields
        assert "account_label" in body
        assert "risk_level" in body
        assert "max_lot_size" in body

    def test_preview_default_symbol_is_xauusd(self, admin_session):
        r = admin_session.get(f"{API}/bot/sizing-preview", timeout=10)
        assert r.status_code == 200
        assert r.json()["symbol"] == "XAUUSD"

    def test_preview_btcusd_uses_btc_scenario(self, admin_session):
        r = admin_session.get(f"{API}/bot/sizing-preview?symbol=BTCUSD", timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body["scenario"]["entry_price"] == 62000.0
        assert body["scenario"]["stop_loss"] == 61500.0
