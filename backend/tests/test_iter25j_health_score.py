from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""Iter 25j — Friendly UX endpoints: Bot Health Score + Quick Actions.

Two new aggregator endpoints power the Dashboard's friendly-UX overhaul:
  • GET /api/bot/health-score — single 0-100 score + actionable advisory list
  • GET /api/bot/quick-actions — compact stats for the sticky action bar

Both must work for any authenticated user, even one with zero accounts.
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
from datetime import datetime, timezone, timedelta
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
                _os.environ.setdefault("MONGO_URL", _strip(line.split("=", 1)[1]))
            if line.startswith("DB_NAME="):
                _os.environ.setdefault("DB_NAME", _strip(line.split("=", 1)[1]))
    client = pymongo.MongoClient(_os.environ["MONGO_URL"])
    return client[_os.environ["DB_NAME"]]


class TestHealthScore:
    def test_health_score_has_required_shape(self, admin_session):
        r = admin_session.get(f"{API}/bot/health-score", timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body["score"], int)
        assert 0 <= body["score"] <= 100
        assert body["status"] in ("excellent", "good", "degraded", "critical")
        assert isinstance(body["issues"], list)
        assert "headline" in body
        assert "checked_at" in body
        # Each issue must have severity / code / label / fix
        for issue in body["issues"]:
            assert issue["severity"] in ("error", "warning", "info")
            assert issue["code"]
            assert issue["label"]
            assert issue["fix"]

    def test_health_score_status_reflects_score_bucket(self, admin_session):
        body = admin_session.get(f"{API}/bot/health-score", timeout=10).json()
        score = body["score"]
        if score >= 90:
            assert body["status"] == "excellent"
        elif score >= 75:
            assert body["status"] == "good"
        elif score >= 50:
            assert body["status"] == "degraded"
        else:
            assert body["status"] == "critical"

    def test_health_score_flags_outdated_ea(self, admin_session, mongo_db):
        """Force one of the admin's connected accounts to report a pre-v1.26
        EA — health score must surface an `ea_outdated` advisory."""
        admin = mongo_db.users.find_one({"email": ADMIN_EMAIL})
        accs = list(mongo_db.accounts.find({
            "user_id": str(admin["_id"]), "status": "connected",
        }))
        if not accs:
            pytest.skip("admin has no connected accounts")
        target = accs[0]
        prior = target.get("ea_version")
        try:
            mongo_db.accounts.update_one(
                {"_id": target["_id"]}, {"$set": {"ea_version": "1.20"}},
            )
            body = admin_session.get(f"{API}/bot/health-score", timeout=10).json()
            codes = [i["code"] for i in body["issues"]]
            assert "ea_outdated" in codes
        finally:
            mongo_db.accounts.update_one(
                {"_id": target["_id"]},
                {"$set": {"ea_version": prior}} if prior else
                {"$unset": {"ea_version": ""}},
            )


class TestQuickActions:
    def test_quick_actions_has_required_shape(self, admin_session):
        r = admin_session.get(f"{API}/bot/quick-actions", timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body["bot_active"], bool)
        assert isinstance(body["open_trades"], int)
        assert isinstance(body["todays_pnl_usd"], (int, float))
        assert isinstance(body["todays_closed_count"], int)

    def test_quick_actions_open_trades_matches_db(self, admin_session, mongo_db):
        admin = mongo_db.users.find_one({"email": ADMIN_EMAIL})
        db_count = mongo_db.trades.count_documents({
            "user_id": str(admin["_id"]), "status": "open",
        })
        body = admin_session.get(f"{API}/bot/quick-actions", timeout=10).json()
        assert body["open_trades"] == db_count


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
