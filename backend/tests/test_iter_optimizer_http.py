"""HTTP integration tests for the AI Strategy Optimizer feature.

PER-ACCOUNT CONTRACT (iter38): analysis requires an account_id, reviews only
that account's bot-executed trades, and Apply writes only to that account's
config (cloning the default profile into a per-account override if needed).

Creates a fresh isolated user + account + fake closed trades in Mongo (so we
NEVER touch the admin's live trading data), exercises the full analyze ->
apply -> dismiss flow, and validates cache + insufficient-data + ownership +
isolation paths.
"""
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import requests
from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
_FRONT_ENV = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontend", ".env")
if os.path.exists(_FRONT_ENV):
    load_dotenv(_FRONT_ENV, override=False)
BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
_mc = MongoClient(MONGO_URL)
db = _mc[DB_NAME]

ACCOUNT_LABEL = "TEST_OPT_ACC"


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def test_user():
    """Create a fresh verified user + isolated Mongo state (no bleed into admin)."""
    email = f"TEST_optimizer_{uuid.uuid4().hex[:8]}@example.com"
    password = "Ce9#Km5tV2xPdG7w"
    r = requests.post(f"{API}/auth/register", json={
        "email": email, "password": password, "name": "Optimizer Test",
        "terms_agreed": True,
    }, timeout=30)
    assert r.status_code in (200, 201), r.text
    lc_email = email.lower()
    db.users.update_one({"email": lc_email}, {"$set": {"email_verified": True}})
    user_doc = db.users.find_one({"email": lc_email})
    assert user_doc, f"user not created for {lc_email}"
    user_id = str(user_doc["_id"])
    yield {"id": user_id, "email": email, "password": password}
    db.users.delete_one({"_id": user_doc["_id"]})
    db.accounts.delete_many({"user_id": user_id})
    db.trades.delete_many({"user_id": user_id})
    db.bot_configs.delete_many({"user_id": user_id})
    db.optimizer_reports.delete_many({"user_id": user_id})


@pytest.fixture(scope="module")
def test_account(test_user):
    """Directly seed an account doc — bridge pairing flow is out of scope here."""
    res = db.accounts.insert_one({
        "user_id": test_user["id"],
        "label": ACCOUNT_LABEL,
        "broker": "TestBroker",
        "server": "Test-Server",
        "account_number": "999999",
        "account_type": "demo",
        "status": "disconnected",
        "bridge_token": f"ITEROPT-{uuid.uuid4().hex[:12]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return str(res.inserted_id)


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


def _seed_closed_trades(user_id: str, account_id: str, n: int = 5,
                        origin: str = "auto"):
    """Insert `n` closed trades within the last 24h for (user, account)."""
    now = datetime.now(timezone.utc)
    docs = []
    for i in range(n):
        opened = now - timedelta(hours=6, minutes=i * 30)
        closed = opened + timedelta(minutes=45)
        pnl = 45.0 if i % 2 == 0 else -30.0
        docs.append({
            "user_id": user_id,
            "account_id": account_id,
            "origin": origin,
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

class TestOptimizerScopeRules:
    def test_analyze_without_account_id_is_400(self, session, test_account):
        r = session.post(f"{API}/optimizer/analyze?window=24", timeout=30)
        assert r.status_code == 400, r.text
        assert "account" in r.json()["detail"].lower()


class TestOptimizerInsufficientData:
    def test_insufficient_data_verdict(self, session, test_user, test_account):
        db.trades.delete_many({"user_id": test_user["id"]})
        db.optimizer_reports.delete_many({"user_id": test_user["id"]})
        r = session.post(f"{API}/optimizer/analyze?account_id={test_account}&window=24",
                         timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("verdict") == "insufficient_data", data
        assert data.get("insufficient_data") is True
        assert data.get("recommendations") == []
        assert data.get("model_used") is None
        assert data.get("account_id") == test_account
        assert data.get("window_hours") == 24

    def test_manual_trades_are_excluded(self, session, test_user, test_account):
        """Manual-origin trades must not count toward MIN_TRADES."""
        db.trades.delete_many({"user_id": test_user["id"]})
        db.optimizer_reports.delete_many({"user_id": test_user["id"]})
        _seed_closed_trades(test_user["id"], test_account, n=5, origin="manual")
        r = session.post(
            f"{API}/optimizer/analyze?account_id={test_account}&window=24&force=true",
            timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("verdict") == "insufficient_data", data
        assert data.get("excluded_manual_trades") == 5, data


class TestOptimizerFullReport:
    """Depends on order — seeds trades then runs analysis; caches state for next tests."""

    def test_analyze_full_report(self, session, test_user, test_account):
        db.trades.delete_many({"user_id": test_user["id"]})
        db.optimizer_reports.delete_many({"user_id": test_user["id"]})
        _seed_closed_trades(test_user["id"], test_account, n=5)
        r = session.post(
            f"{API}/optimizer/analyze?account_id={test_account}&window=24",
            timeout=120)
        assert r.status_code == 200, r.text
        data = r.json()
        TestOptimizerFullReport.report = data

        assert data.get("verdict") in ("healthy", "needs_tuning", "underperforming",
                                       "critical", "unavailable")
        if data["verdict"] == "unavailable":
            pytest.skip("LLM unavailable — recommendation-flow tests need a healthy LLM.")

        assert data.get("account_id") == test_account
        assert isinstance(data.get("headline"), str) and data["headline"]
        stats = data.get("stats", {})
        assert stats.get("total_trades") == 5
        assert "win_rate" in stats and "profit_factor" in stats
        assert "by_symbol" in stats and "by_session" in stats
        assert isinstance(data.get("patterns"), list)
        recs = data.get("recommendations")
        assert isinstance(recs, list)
        for rec in recs:
            assert rec.get("status") == "pending"
            assert rec.get("id")
            assert rec.get("type") in ("config_change", "preset_switch", "pause_bot")
        assert data.get("model_used") in ("claude-fable-5", "claude-opus-4-8")

    def test_cached_within_5min(self, session, test_account):
        if not getattr(TestOptimizerFullReport, "report", None):
            pytest.skip("no prior report")
        prev = TestOptimizerFullReport.report
        r = session.post(
            f"{API}/optimizer/analyze?account_id={test_account}&window=24", timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert data.get("cached") is True, data
        assert data.get("id") == prev["id"]

    def test_summary_endpoint(self, session, test_account):
        r = session.get(f"{API}/optimizer/summary", timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert "reports" in data and "total_pending" in data
        rows = [x for x in data["reports"] if x["account_id"] == test_account]
        assert len(rows) == 1, data
        assert rows[0]["account_label"] == ACCOUNT_LABEL
        # per-account only — no default-scope (null) rows may appear
        assert all(x["account_id"] for x in data["reports"])

    def test_report_endpoint(self, session, test_account):
        r = session.get(f"{API}/optimizer/report?account_id={test_account}", timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert data.get("account_id") == test_account
        assert data.get("verdict") in ("healthy", "needs_tuning", "underperforming",
                                       "critical", "unavailable", "insufficient_data")

    def test_apply_is_isolated_to_account(self, session, test_user, test_account):
        """Apply must (a) persist on the per-account config and (b) NEVER
        mutate the user's default profile."""
        rep = getattr(TestOptimizerFullReport, "report", None)
        if not rep or not rep.get("recommendations"):
            pytest.skip("no recommendations to apply")

        report_id = rep["id"]
        cfg_recs = [r for r in rep["recommendations"] if r["type"] == "config_change"]
        if cfg_recs:
            target = cfg_recs[0]
            # Snapshot the DEFAULT profile before apply
            default_before = session.get(f"{API}/bot/config", timeout=30).json()

            r = session.post(
                f"{API}/optimizer/report/{report_id}/rec/{target['id']}/apply",
                timeout=30)
            assert r.status_code == 200, r.text
            assert r.json().get("applied") is True

            # Per-account config carries the change
            after = session.get(
                f"{API}/bot/config?account_id={test_account}", timeout=30).json()
            assert after.get(target["field"]) == target["to"], (
                f"expected {target['to']} got {after.get(target['field'])}")
            assert after.get("account_id") == test_account

            # Default profile untouched
            default_after = session.get(f"{API}/bot/config", timeout=30).json()
            assert default_after.get(target["field"]) == default_before.get(target["field"]), (
                "apply leaked into the default profile!")

            # Second apply → 409 ; dismiss applied → 409
            assert session.post(
                f"{API}/optimizer/report/{report_id}/rec/{target['id']}/apply",
                timeout=30).status_code == 409
            assert session.post(
                f"{API}/optimizer/report/{report_id}/rec/{target['id']}/dismiss",
                timeout=30).status_code == 409

        pending = [r for r in rep["recommendations"]
                   if r["status"] == "pending"
                   and (not cfg_recs or r["id"] != cfg_recs[0]["id"])]
        if pending:
            rec = pending[0]
            r = session.post(
                f"{API}/optimizer/report/{report_id}/rec/{rec['id']}/dismiss",
                timeout=30)
            assert r.status_code == 200, r.text
            assert session.post(
                f"{API}/optimizer/report/{report_id}/rec/{rec['id']}/apply",
                timeout=30).status_code == 409


class TestOptimizerOwnership:
    def test_analyze_with_other_users_account_returns_404(self, session, admin_session):
        r = admin_session.get(f"{API}/accounts", timeout=30)
        if r.status_code != 200 or not r.json():
            pytest.skip("admin has no accounts to use for cross-owner test")
        other_account_id = r.json()[0]["id"]
        r = session.post(f"{API}/optimizer/analyze?account_id={other_account_id}",
                         timeout=30)
        assert r.status_code == 404, r.status_code


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
