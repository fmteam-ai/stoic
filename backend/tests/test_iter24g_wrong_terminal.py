"""Iter 24g — Wrong-terminal detection + /test-connection URL fix.

Bugs being fixed:
  1. Frontend called /test_connection (underscore) → backend was /test-connection
     (hyphen) → 404 → "Refresh failed, not found" toast.
  2. Two EAs attached to the same MT5 instance reported the same balance
     across two STOIC accounts. Solved by having the EA include the broker's
     ACCOUNT_LOGIN in every heartbeat and the backend flagging mismatches.

Contracts under test:
  - GET /api/accounts/{id}/test-connection responds 200 (route exists)
  - POST /api/bridge/heartbeat with account_login != configured account_number
    sets broker_account_mismatch=True and a clear diagnostic message
  - Heartbeat with matching login clears the flag
  - Test-connection elevates severity to "warn" when mismatch is present
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
def live_account(admin_session, mongo_db):
    """A live account belonging to admin that we can poke heartbeats at."""
    accs = admin_session.get(f"{API}/accounts", timeout=10).json()
    live = next((a for a in accs if a.get("mode") == "live"), None)
    if not live:
        pytest.skip("admin has no live MT5 account")
    from bson import ObjectId
    full = mongo_db.accounts.find_one({"_id": ObjectId(live["id"])})
    return full


class TestWrongTerminalDetection:
    def test_test_connection_route_responds(self, admin_session, live_account):
        """The hyphen vs underscore typo is fixed — route must respond 200."""
        aid = str(live_account["_id"])
        r = admin_session.get(f"{API}/accounts/{aid}/test-connection", timeout=10)
        assert r.status_code == 200

    def test_heartbeat_flags_mismatch(self, admin_session, live_account, mongo_db):
        """When the EA reports a different MT5 login than configured, the
        backend must persist broker_account_mismatch=True with a message."""
        token = live_account["bridge_token"]
        configured = str(live_account.get("account_number"))
        bogus_login = int(configured) + 999_999_999  # guaranteed mismatch

        # Send heartbeat with WRONG login
        r = requests.post(f"{API}/bridge/heartbeat", json={
            "bridge_token": token,
            "balance": live_account.get("balance") or 1000.0,
            "equity": live_account.get("equity") or 1000.0,
            "open_positions": 0,
            "account_login": bogus_login,
            "base_currency": "USD",
        }, timeout=10)
        assert r.status_code == 200

        # /test-connection must surface the warning
        aid = str(live_account["_id"])
        body = admin_session.get(f"{API}/accounts/{aid}/test-connection", timeout=10).json()
        assert body["broker_account_mismatch"] is True
        assert body["broker_account_id_reported"] == bogus_login
        assert configured in body["broker_account_mismatch_reason"]
        assert str(bogus_login) in body["broker_account_mismatch_reason"]
        assert body["diagnostic"]["severity"] in ("warn", "error")

    def test_correct_login_clears_mismatch(self, admin_session, live_account):
        token = live_account["bridge_token"]
        configured = int(live_account["account_number"])

        r = requests.post(f"{API}/bridge/heartbeat", json={
            "bridge_token": token,
            "balance": live_account.get("balance") or 1000.0,
            "equity": live_account.get("equity") or 1000.0,
            "open_positions": 0,
            "account_login": configured,
            "base_currency": "USD",
        }, timeout=10)
        assert r.status_code == 200
        aid = str(live_account["_id"])
        body = admin_session.get(f"{API}/accounts/{aid}/test-connection", timeout=10).json()
        assert body["broker_account_mismatch"] is False

    def test_heartbeat_without_login_keeps_prior_state(self, admin_session, live_account):
        """Legacy EA v1.23 (no account_login in heartbeat) must not crash."""
        r = requests.post(f"{API}/bridge/heartbeat", json={
            "bridge_token": live_account["bridge_token"],
            "balance": 1000.0, "equity": 1000.0, "open_positions": 0,
        }, timeout=10)
        assert r.status_code == 200


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
