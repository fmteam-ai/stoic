from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""Iter 24j — Manual import open positions endpoint.

POST /api/accounts/{id}/import-positions lets the user paste open positions
from MT5's Trade tab when their EA is on a legacy version (pre-v1.25). Once
v1.25 is installed the heartbeat snapshot does this automatically.

Contracts:
  - Inserts new trade docs as origin=external, manually_imported=True
  - Idempotent on (account_id, mt5_ticket) — skips existing tickets
  - Unknown account → 404
  - Validates type ∈ {BUY,SELL} and volume > 0 + price_open > 0 (Pydantic)
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

TBASE = 88000000


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
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
def account_id(admin_session):
    accs = admin_session.get(f"{API}/accounts", timeout=10).json()
    return accs[0]["id"] if accs else None


class TestImportPositions:
    def test_import_creates_trades(self, admin_session, account_id, mongo_db):
        tickets = [TBASE + 1, TBASE + 2]
        try:
            r = admin_session.post(
                f"{API}/accounts/{account_id}/import-positions",
                json={"positions": [
                    {"ticket": TBASE + 1, "symbol": "XAUUSD", "type": "BUY",
                     "volume": 0.5, "price_open": 4050.1, "sl": 4030, "tp": 4100},
                    {"ticket": TBASE + 2, "symbol": "BTCUSD", "type": "SELL",
                     "volume": 0.01, "price_open": 62000},
                ]}, timeout=10)
            assert r.status_code == 200
            body = r.json()
            assert body["created"] == 2
            for t in tickets:
                doc = mongo_db.trades.find_one({"mt5_ticket": t})
                assert doc is not None
                assert doc["status"] == "open"
                assert doc["origin"] == "external"
                assert doc["manually_imported"] is True
        finally:
            mongo_db.trades.delete_many({"mt5_ticket": {"$in": tickets}})

    def test_idempotent_skips_existing(self, admin_session, account_id, mongo_db):
        ticket = TBASE + 3
        try:
            payload = {"positions": [
                {"ticket": ticket, "symbol": "XAUUSD", "type": "BUY",
                 "volume": 0.1, "price_open": 4050},
            ]}
            r1 = admin_session.post(f"{API}/accounts/{account_id}/import-positions",
                                    json=payload, timeout=10)
            assert r1.json()["created"] == 1
            r2 = admin_session.post(f"{API}/accounts/{account_id}/import-positions",
                                    json=payload, timeout=10)
            assert r2.json()["created"] == 0
            assert ticket in r2.json()["skipped_existing"]
            assert mongo_db.trades.count_documents({"mt5_ticket": ticket}) == 1
        finally:
            mongo_db.trades.delete_many({"mt5_ticket": ticket})

    def test_unknown_account_404(self, admin_session):
        r = admin_session.post(
            f"{API}/accounts/000000000000000000000000/import-positions",
            json={"positions": [
                {"ticket": 1, "symbol": "X", "type": "BUY",
                 "volume": 0.1, "price_open": 1.0},
            ]}, timeout=10)
        assert r.status_code == 404

    def test_validation_rejects_invalid_type(self, admin_session, account_id):
        r = admin_session.post(
            f"{API}/accounts/{account_id}/import-positions",
            json={"positions": [
                {"ticket": 1, "symbol": "X", "type": "HOLD",
                 "volume": 0.1, "price_open": 1.0},
            ]}, timeout=10)
        assert r.status_code == 422

    def test_validation_rejects_zero_volume(self, admin_session, account_id):
        r = admin_session.post(
            f"{API}/accounts/{account_id}/import-positions",
            json={"positions": [
                {"ticket": 1, "symbol": "X", "type": "BUY",
                 "volume": 0, "price_open": 1.0},
            ]}, timeout=10)
        assert r.status_code == 422


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
