"""Iter 25f — EA `client_version` propagation.

Heartbeat carries the EA's semantic version (v1.26+). Backend persists it
on the account doc so the Dashboard's EA Version strip can highlight stale
terminals that the user needs to recompile.
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
from bson import ObjectId

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
    env_path = pathlib.Path(_os.path.join(_BACKEND_DIR, ".env"))
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
    """A throw-away account doc with a bridge_token for heartbeat testing."""
    acc = {
        "user_id": me["id"],
        "label": "TEST_iter25f", "broker": "RoboForex",
        "account_type": "demo", "account_number": "TESTACC",
        "bridge_token": "iter25f-tok-" + str(ObjectId()),
        "status": "disconnected", "balance": 0, "equity": 0,
        "mode": "live", "created_at": datetime.now(timezone.utc).isoformat(),
    }
    res = mongo_db.accounts.insert_one(acc)
    acc["_id"] = res.inserted_id
    yield acc
    mongo_db.accounts.delete_one({"_id": acc["_id"]})


class TestEaVersionPropagation:
    def test_heartbeat_persists_client_version(self, mongo_db, fresh_account):
        r = requests.post(f"{API}/bridge/heartbeat", json={
            "bridge_token": fresh_account["bridge_token"],
            "balance": 1000.0, "equity": 1000.0, "open_positions": 0,
            "client_version": "1.26",
        }, timeout=10)
        assert r.status_code == 200, r.text
        doc = mongo_db.accounts.find_one({"_id": fresh_account["_id"]})
        assert doc["ea_version"] == "1.26"
        assert doc.get("ea_version_updated_at")

    def test_heartbeat_without_version_leaves_field_unset(
        self, mongo_db, fresh_account
    ):
        r = requests.post(f"{API}/bridge/heartbeat", json={
            "bridge_token": fresh_account["bridge_token"],
            "balance": 1000.0, "equity": 1000.0, "open_positions": 0,
        }, timeout=10)
        assert r.status_code == 200
        doc = mongo_db.accounts.find_one({"_id": fresh_account["_id"]})
        # ea_version absent → legacy EA → frontend marks as "OLD EA · UPGRADE"
        assert doc.get("ea_version") is None

    def test_list_accounts_exposes_ea_version(
        self, admin_session, mongo_db, fresh_account
    ):
        # First push a heartbeat with version
        requests.post(f"{API}/bridge/heartbeat", json={
            "bridge_token": fresh_account["bridge_token"],
            "balance": 1000.0, "equity": 1000.0, "open_positions": 0,
            "client_version": "1.26",
        }, timeout=10)
        # Then list accounts and verify the field is returned
        accs = admin_session.get(f"{API}/accounts", timeout=10).json()
        ours = next((a for a in accs if a["id"] == str(fresh_account["_id"])), None)
        assert ours is not None, "test account missing from /accounts response"
        assert ours.get("ea_version") == "1.26"
