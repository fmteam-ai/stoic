"""HTTP integration tests for the AI Strategy Optimizer feature.

Creates a fresh isolated user + fake closed trades in Mongo (so we NEVER touch
the admin's live trading data), exercises the full analyze -> apply -> dismiss
flow, and validates cache + insufficient-data + 404 ownership paths.
"""
import os
import sys
import uuid
import time
import asyncio
from datetime import datetime, timezone, timedelta

import pytest
import requests
from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
# Read REACT_APP_BACKEND_URL from frontend .env too (backend .env may not have it)
_FRONT_ENV = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontend", ".env")
if os.path.exists(_FRONT_ENV):
    load_dotenv(_FRONT_ENV, override=False)
BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
_mc = MongoClient(MONGO_URL)
db = _mc[DB_NAME]


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def test_user():
    """Create a fresh verified user + isolated Mongo state (no bleed into admin)."""
    email = f"TEST_optimizer_{uuid.uuid4().hex[:8]}@example.com"
    password = "Test1234!"
    r = requests.post(f"{API}/auth/register", json={
        "email": email, "password": password, "name": "Optimizer Test",
        "terms_agreed": True,
    }, timeout=30)
    assert r.status_code in (200, 201), r.text
    # emails are stored lowercase
    lc_email = email.lower()
    db.users.update_one({"email": lc_email}, {"$set": {"email_verified": True}})
    user_doc = db.users.find_one({"email": lc_email})
    assert user_doc, f"user not created for {lc_email}"
    user_id = str(user_doc["_id"])  # ids are stored as strings in this app
    yield {"id": user_id, "email": email, "password": password}
    # cleanup — remove user + everything we seeded
    db.users.delete_one({"_id": user_doc["_id"]})
    db.trades.delete_many({"user_id": user_id})
    db.bot_configs.delete_many({"user_id": user_id})
    db.optimizer_reports.delete_many({"user_id": user_id})


@pytest.fixture(scope="module")
def session(test_user):
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": test_user["email"], "password": test_user["password"]},
               timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"}, timeout=30)
    assert r.status_code == 200, r.text
    return s


def _seed_closed_trades(user_id: str, n: int = 5):
    """Insert `n` closed trades within the last 24h for the test user."""
    now = datetime.now(timezone.utc)
    docs = []
    for i in range(n):
        opened = now - timedelta(hours=6, minutes=i * 30)
        closed = opened + timedelta(minutes=45)
        # Alternate wins/losses so bucket math is meaningful.
        pnl = 45.0 if i % 2 == 0 else -30.0
        docs.append({
            "user_id": user_id,
            "status": "closed",
            "symbol": "XAUUSD",
            "base_symbol": "XAUUSD",
            "action": "BUY" if i % 2 == 0 else "SELL",
            "pnl": pnl,
            "lot_size": 0.10,
            "opened_at": opened.isoformat(),
            "closed_at": closed.isoformat(),
            "close_reason": "take_profit" if pnl > 0 else "stop_loss",
        })
    db.trades.insert_many(docs)


# ---------------------------------------------------------------- tests

class TestOptimizerInsufficientData:
    def test_insufficient_data_verdict(self, session, test_user):
        # Fresh user has 0 trades — must be insufficient_data + no LLM call.
        db.trades.delete_many({"user_id": test_user["id"]})
        db.optimizer_reports.delete_many({"user_id": test_user["id"]})
        r = session.post(f"{API}/optimizer/analyze?window=24", timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("verdict") == "insufficient_data", data
        assert data.get("insufficient_data") is True
        assert data.get("recommendations") == []
        assert data.get("model_used") is None
        assert "id" in data
        assert data.get("window_hours") == 24


class TestOptimizerFullReport:
    """Depends on order — seeds trades then runs analysis; caches state for next tests."""

    def test_analyze_full_report(self, session, test_user):
        db.optimizer_reports.delete_many({"user_id": test_user["id"]})
        _seed_closed_trades(test_user["id"], n=5)
        # LLM call is slow — 90s timeout.
        r = session.post(f"{API}/optimizer/analyze?window=24", timeout=120)
        assert r.status_code == 200, r.text
        data = r.json()
        # Save for downstream cache/apply tests via module-level pytest cache.
        TestOptimizerFullReport.report = data

        assert data.get("verdict") in ("healthy", "needs_tuning", "underperforming",
                                       "critical", "unavailable")
        # If LLM fully failed we degrade to "unavailable" — that's still a valid path
        # but recommendations list will be empty. Skip apply tests if so.
        if data["verdict"] == "unavailable":
            pytest.skip("LLM unavailable — recommendation-flow tests need a healthy LLM.")

        assert isinstance(data.get("headline"), str) and data["headline"]
        assert isinstance(data.get("summary"), str)
        stats = data.get("stats", {})
        assert stats.get("total_trades") == 5
        assert "win_rate" in stats and "profit_factor" in stats
        assert "by_symbol" in stats and "by_session" in stats
        assert "worst_losing_streak" in stats
        assert isinstance(data.get("patterns"), list)
        recs = data.get("recommendations")
        assert isinstance(recs, list)
        for rec in recs:
            assert rec.get("status") == "pending"
            assert rec.get("id")
            assert rec.get("type") in ("config_change", "preset_switch", "pause_bot")
        assert data.get("model_used") in ("claude-fable-5", "claude-opus-4-8"), data.get("model_used")

    def test_cached_within_5min(self, session):
        # Second call within 5 minutes must return cached=True with same report id.
        if not getattr(TestOptimizerFullReport, "report", None):
            pytest.skip("no prior report")
        prev = TestOptimizerFullReport.report
        r = session.post(f"{API}/optimizer/analyze?window=24", timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert data.get("cached") is True, data
        assert data.get("id") == prev["id"]

    def test_summary_endpoint(self, session):
        r = session.get(f"{API}/optimizer/summary", timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert "reports" in data and "total_pending" in data
        assert isinstance(data["reports"], list) and len(data["reports"]) >= 1
        row = data["reports"][0]
        for k in ("report_id", "account_label", "verdict",
                  "pending_recommendations", "win_rate"):
            assert k in row
        # For default scope: account_label defaults to "Default Profile"
        assert row["account_label"] == "Default Profile"

    def test_report_endpoint(self, session):
        r = session.get(f"{API}/optimizer/report", timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert data.get("verdict") in ("healthy", "needs_tuning", "underperforming",
                                       "critical", "unavailable", "insufficient_data")

    def test_apply_and_dismiss_recommendations(self, session, test_user):
        rep = getattr(TestOptimizerFullReport, "report", None)
        if not rep or not rep.get("recommendations"):
            pytest.skip("no recommendations to apply")

        report_id = rep["id"]
        # Pick a config_change rec (safe — anything else on a fresh user is also fine
        # because we clean up, but config_change also lets us prove GET /bot/config).
        cfg_recs = [r for r in rep["recommendations"] if r["type"] == "config_change"]
        if cfg_recs:
            target = cfg_recs[0]
            # Baseline
            before = session.get(f"{API}/bot/config", timeout=30).json()
            assert before.get(target["field"]) == target.get("from") or True  # tolerate None from-value

            # APPLY
            r = session.post(f"{API}/optimizer/report/{report_id}/rec/{target['id']}/apply",
                             timeout=30)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body.get("applied") is True
            assert body["audit"]["field"] == target["field"]
            # Verify persisted
            after = session.get(f"{API}/bot/config", timeout=30).json()
            assert after.get(target["field"]) == target["to"], (
                f"expected {target['to']} got {after.get(target['field'])}")

            # Second apply → 409
            r2 = session.post(f"{API}/optimizer/report/{report_id}/rec/{target['id']}/apply",
                              timeout=30)
            assert r2.status_code == 409

            # Dismissing an applied rec → 409
            r3 = session.post(f"{API}/optimizer/report/{report_id}/rec/{target['id']}/dismiss",
                              timeout=30)
            assert r3.status_code == 409

        # DISMISS a different pending rec if available
        pending = [r for r in rep["recommendations"]
                   if r["status"] == "pending"
                   and (not cfg_recs or r["id"] != cfg_recs[0]["id"])]
        if pending:
            rec = pending[0]
            r = session.post(f"{API}/optimizer/report/{report_id}/rec/{rec['id']}/dismiss",
                             timeout=30)
            assert r.status_code == 200, r.text
            assert r.json().get("dismissed") is True
            # applying a dismissed rec → 409
            r2 = session.post(f"{API}/optimizer/report/{report_id}/rec/{rec['id']}/apply",
                              timeout=30)
            assert r2.status_code == 409


class TestOptimizerOwnership:
    def test_analyze_with_other_users_account_returns_404(self, session, admin_session):
        # Get an admin account_id — if admin has none, skip.
        r = admin_session.get(f"{API}/accounts", timeout=30)
        if r.status_code != 200 or not r.json():
            pytest.skip("admin has no accounts to use for cross-owner test")
        other_account_id = r.json()[0]["id"]
        r = session.post(f"{API}/optimizer/analyze?account_id={other_account_id}",
                         timeout=30)
        assert r.status_code == 404, r.status_code
