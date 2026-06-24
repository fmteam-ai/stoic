"""Iter 25 — Revive must clear pending_modification.

Regression: a trade wrongly closed by the reconciler (close_requested=True,
pending_modification=FULL_CLOSE) and then revived must come back with NO
pending modification — otherwise the EA's next /poll-trades would
immediately close it again.
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


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=15)
    assert r.status_code == 200
    return s


def _strip(v: str) -> str:
    return v.strip().strip('"').strip("'")


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


class TestReviveClearsPendingModification:
    def test_revive_clears_pending_full_close(
        self, admin_session, mongo_db, me, account_id
    ):
        """Trade reconciler wrongly closed a slippage-vetoed trade. Revive
        must wipe pending_modification + close_requested + close_reason so
        the EA doesn't immediately re-kill it on the next poll-trades tick."""
        if not account_id:
            pytest.skip("no account")
        now = datetime.now(timezone.utc).isoformat()
        tid = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": account_id,
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.5,
            "original_lot_size": 0.5,
            "entry_price": 4076.83, "stop_loss": 4094.4, "take_profit": 4049.4,
            "status": "closed", "exit_price": None,   # closed but no exit_price
            "close_reason": "broker_reconciled_manual_force",
            "close_requested": True,
            "pending_modification": {"type": "FULL_CLOSE"},
            "mt5_ticket": 70000900, "opened_at": now, "closed_at": now,
        }).inserted_id
        try:
            r = admin_session.post(f"{API}/trades/{tid}/revive", timeout=10)
            assert r.status_code == 200, r.text
            doc = mongo_db.trades.find_one({"_id": tid})
            assert doc["status"] == "open"
            assert doc.get("pending_modification") in (None,), \
                f"pending_modification must be cleared on revive, got {doc.get('pending_modification')}"
            assert "close_requested" not in doc or not doc.get("close_requested")
            assert "close_reason" not in doc or not doc.get("close_reason")
            assert doc.get("revived_at")
        finally:
            mongo_db.trades.delete_one({"_id": tid})

    def test_audit_shows_pending_modification(
        self, admin_session, mongo_db, me, account_id
    ):
        """Stuck pending partial-close should be visible in /audit so the
        user can diagnose why the trade isn't progressing."""
        if not account_id:
            pytest.skip("no account")
        now = datetime.now(timezone.utc).isoformat()
        tid = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": account_id,
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 1.0,
            "entry_price": 4100.0, "stop_loss": 4115.0, "take_profit": 4070.0,
            "status": "open", "mt5_ticket": 70000901, "opened_at": now,
            "pending_modification": {
                "type": "PARTIAL_CLOSE", "new_volume": 0.5,
                "new_sl": 4100.0, "requested_at": now,
            },
        }).inserted_id
        try:
            r = admin_session.get(f"{API}/trades/{tid}/audit", timeout=10)
            assert r.status_code == 200, r.text
            kinds = [e["kind"] for e in r.json()["events"]]
            assert "pending_modification" in kinds
        finally:
            mongo_db.trades.delete_one({"_id": tid})
