"""iter-211 — HTTP end-to-end tests for the NEW/CHANGED brain + latency
endpoints introduced by the 10 architectural corrections.

Covers:
  • /api/latency/summary            (p50/p95/p99/max/n/unknown_rate keys)
  • /api/latency/clock-skew         (accounts/suspected/note shape)
  • /api/brain/health               (global / regional / account + BOLA)
  • /api/brain/report               (unified Trade Intelligence Report)
  • /api/brain/interventions        (effectiveness metrics)
  • /api/brain/coverage             (realized conformal coverage)
  • 401 auth-guard on every new endpoint
  • data-backed report/interventions (seeded closed trades + meta docs)
  • tenant isolation (user A cannot see user B's data)

All tests are HTTP (marker `http`); they run against the live preview
backend via REACT_APP_BACKEND_URL and hit real MongoDB (via MONGO_URL).
"""
import os
import time
import uuid

import pytest
import requests
from bson import ObjectId
from pymongo import MongoClient

pytestmark = pytest.mark.http

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


# ─────────────────────────── fixtures ────────────────────────────────────


@pytest.fixture(scope="module")
def mongo():
    c = MongoClient(MONGO_URL)
    yield c[DB_NAME]
    c.close()


def _login(session: requests.Session, email: str, password: str) -> dict:
    r = session.post(f"{BASE_URL}/api/auth/login",
                     json={"email": email, "password": password},
                     timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    _login(s, ADMIN_EMAIL, ADMIN_PASSWORD)
    return s


def _make_verified_user(mongo) -> tuple[str, str, str]:
    """Register a fresh user via /api/auth/register and flip
    email_verified=true directly in Mongo, per test_credentials.md."""
    email = f"test_iter211_{uuid.uuid4().hex[:10]}@example.com"
    password = f"Iter211-{uuid.uuid4().hex[:12]}-Zx"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": password,
                            "name": "Iter211 Tester",
                            "terms_agreed": True}, timeout=15)
    assert r.status_code in (200, 201), f"register: {r.status_code} {r.text}"
    mongo.users.update_one({"email": email},
                           {"$set": {"email_verified": True}})
    user_doc = mongo.users.find_one({"email": email})
    assert user_doc, "user not persisted"
    return email, password, str(user_doc["_id"])


@pytest.fixture(scope="module")
def user_a(mongo):
    email, password, uid = _make_verified_user(mongo)
    s = requests.Session()
    _login(s, email, password)
    yield {"session": s, "email": email, "password": password, "id": uid}
    mongo.users.delete_one({"email": email})
    mongo.accounts.delete_many({"user_id": uid})
    mongo.trades.delete_many({"user_id": uid})
    mongo.meta_decisions.delete_many({"user_id": uid})
    mongo.decision_contexts.delete_many({"user_id": uid})


@pytest.fixture(scope="module")
def user_b(mongo):
    email, password, uid = _make_verified_user(mongo)
    s = requests.Session()
    _login(s, email, password)
    yield {"session": s, "email": email, "password": password, "id": uid}
    mongo.users.delete_one({"email": email})
    mongo.accounts.delete_many({"user_id": uid})


@pytest.fixture(scope="module")
def user_a_account(mongo, user_a):
    acc_id = ObjectId()
    mongo.accounts.insert_one({
        "_id": acc_id, "user_id": user_a["id"],
        "broker": "TestBrokerA", "server": "TestBrokerA-Demo",
        "mode": "paper", "equity": 10000.0,
        "bridge_token": f"bt_{uuid.uuid4().hex}",
        "last_heartbeat": "2999-01-01T00:00:00+00:00"})
    yield str(acc_id)
    mongo.accounts.delete_one({"_id": acc_id})


@pytest.fixture(scope="module")
def user_b_account(mongo, user_b):
    acc_id = ObjectId()
    mongo.accounts.insert_one({
        "_id": acc_id, "user_id": user_b["id"],
        "broker": "TestBrokerB", "server": "TestBrokerB-Demo",
        "mode": "paper", "equity": 10000.0,
        "bridge_token": f"bt_{uuid.uuid4().hex}",
        "last_heartbeat": "2999-01-01T00:00:00+00:00"})
    yield str(acc_id)
    mongo.accounts.delete_one({"_id": acc_id})


# ─────────────────── /api/latency/summary shape ─────────────────────────


class TestLatencySummary:
    def test_admin_summary_shape(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/latency/summary?days=7",
                              timeout=20)
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ("days", "traced_trades", "total_trades",
                  "unknown_rate", "groups"):
            assert k in body, f"missing top-level key {k}: {body.keys()}"
        assert isinstance(body["groups"], list)
        # If any group is present, verify per-segment stat shape
        if body["groups"]:
            g = body["groups"][0]
            for stat_key in ("strategy_ms", "risk_authority_ms",
                             "cloud_to_ea_ms", "ea_processing_ms",
                             "broker_ms", "total_ms"):
                assert stat_key in g, f"segment {stat_key} missing"
                st = g[stat_key]
                for f in ("p50", "p95", "p99", "max", "n", "unknown_rate"):
                    assert f in st, f"stat field {f} missing in {stat_key}"

    def test_summary_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/latency/summary", timeout=10)
        assert r.status_code == 401


# ─────────────────── /api/latency/clock-skew shape ──────────────────────


class TestClockSkew:
    def test_admin_shape(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/latency/clock-skew",
                              timeout=20)
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ("days", "accounts", "suspected", "note"):
            assert k in body, f"missing {k}"
        assert isinstance(body["accounts"], list)
        assert isinstance(body["suspected"], list)
        for row in body["accounts"]:
            for f in ("account_id", "cloud_to_ea_min_ms",
                      "cloud_to_ea_median_ms", "skew_bound_ms", "status"):
                assert f in row, f"account row missing {f}"
            assert row["status"] in ("OK", "SKEW_SUSPECTED")

    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/latency/clock-skew", timeout=10)
        assert r.status_code == 401


# ────────────────────── /api/brain/health scopes ────────────────────────


class TestBrainHealth:
    def test_global_admin_has_last_error_key_allowed(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/brain/health?scope=global",
                              timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("scope") == "global"
        assert body.get("status") in ("GREEN", "YELLOW", "RED")
        assert "mode" in body
        assert "subsystems" in body and isinstance(body["subsystems"], dict)

    def test_global_non_admin_strips_last_error(self, user_a):
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/health?scope=global", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        for sub in body.get("subsystems", {}).values():
            assert "last_error" not in sub, (
                "non-admin must NOT see last_error in subsystems")

    def test_regional_shape(self, user_a, user_a_account):
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/health?scope=regional", timeout=20)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("scope") == "regional"
        assert isinstance(body.get("regions"), list)
        # user A owns exactly ONE account with broker TestBrokerA
        regions = {row["region"] for row in body["regions"]}
        assert "TestBrokerA" in regions, (
            f"regional health did not include user's broker; got {regions}")
        # tenant isolation — must not see user B's broker
        assert "TestBrokerB" not in regions
        for row in body["regions"]:
            for f in ("region", "accounts", "connected",
                      "worst_p95_total_ms", "status"):
                assert f in row, f"regional row missing {f}"
            assert row["status"] in ("GREEN", "YELLOW", "RED")

    def test_account_scope_happy_path(self, user_a, user_a_account):
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/health?scope=account"
            f"&account_id={user_a_account}", timeout=20)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("scope") == "account"
        assert body.get("account_id") == user_a_account
        for f in ("connected", "clock_skew", "strategy_decay", "status"):
            assert f in body, f"account body missing {f}"

    def test_account_scope_missing_id_400(self, user_a):
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/health?scope=account", timeout=10)
        assert r.status_code == 400

    def test_account_bola_returns_404(self, user_a, user_b_account):
        """User A must NOT be able to read user B's account health."""
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/health?scope=account"
            f"&account_id={user_b_account}", timeout=10)
        assert r.status_code == 404, (
            f"BOLA leak: user A got {r.status_code} on user B's account; "
            f"body={r.text}")

    def test_invalid_scope_400(self, user_a):
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/health?scope=nonsense", timeout=10)
        assert r.status_code == 400

    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/brain/health", timeout=10)
        assert r.status_code == 401


# ────────────────────── /api/brain/report shape ─────────────────────────


class TestBrainReport:
    def test_report_shape(self, user_a):
        r = user_a["session"].get(f"{BASE_URL}/api/brain/report?days=30",
                                  timeout=30)
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ("days", "user_id", "generated_at", "funnel", "outcomes",
                  "execution", "uncertainty_calibration", "interventions",
                  "learning"):
            assert k in body, f"report missing {k}"
        funnel = body["funnel"]
        assert set(funnel.keys()) >= {"decisions_minted", "meta",
                                      "trades_opened"}
        assert set(funnel["meta"].keys()) >= {"TRADE", "REDUCE", "SKIP"}
        outcomes = body["outcomes"]
        for f in ("closed", "win_rate", "avg_r", "total_r", "total_pnl"):
            assert f in outcomes
        execution = body["execution"]
        for f in ("traced_trades", "unknown_rate", "worst_groups"):
            assert f in execution
        learning = body["learning"]
        for f in ("strategy_health", "degraded_mode"):
            assert f in learning

    def test_days_validation_zero(self, user_a):
        r = user_a["session"].get(f"{BASE_URL}/api/brain/report?days=0",
                                  timeout=10)
        assert r.status_code == 422

    def test_days_validation_91(self, user_a):
        r = user_a["session"].get(f"{BASE_URL}/api/brain/report?days=91",
                                  timeout=10)
        assert r.status_code == 422

    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/brain/report", timeout=10)
        assert r.status_code == 401


# ─────────────────── /api/brain/interventions shape ─────────────────────


class TestBrainInterventions:
    def test_shape(self, user_a):
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/interventions?days=30", timeout=20)
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ("days", "meta", "twin", "execution_alpha_modes",
                  "reduce_effect", "skips"):
            assert k in body, f"interventions missing {k}"
        assert set(body["meta"].keys()) >= {"counts", "hard_gates"}
        assert set(body["twin"].keys()) >= {"TRADE", "REDUCE", "SKIP"}
        re = body["reduce_effect"]
        for f in ("trade_cohort", "reduce_cohort", "saved_on_losers_usd",
                  "forgone_on_winners_usd", "net_usd",
                  "downgrades_justified"):
            assert f in re, f"reduce_effect missing {f}"
        # honest note about SKIP
        assert "note" in body["skips"]
        assert "counterfactual" in body["skips"]["note"]

    def test_days_validation(self, user_a):
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/interventions?days=0", timeout=10)
        assert r.status_code == 422

    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/brain/interventions", timeout=10)
        assert r.status_code == 401


# ────────────────────── /api/brain/coverage shape ───────────────────────


class TestBrainCoverage:
    def test_shape_insufficient(self, user_a):
        # brand-new user has no comparable history — insufficient form
        r = user_a["session"].get(f"{BASE_URL}/api/brain/coverage",
                                  timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("evaluated") == 0
        assert body.get("coverage") is None
        assert body.get("ok") is True
        assert "note" in body

    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/brain/coverage", timeout=10)
        assert r.status_code == 401


# ────────────────── data-backed report / interventions ──────────────────


@pytest.fixture(scope="module")
def seeded_user(mongo):
    """User C — same shape as user_a but with seeded closed trades and
    meta_decisions so the report / interventions carry non-zero cohorts."""
    email, password, uid = _make_verified_user(mongo)
    s = requests.Session()
    _login(s, email, password)
    # seed decision_contexts (minted decisions)
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    dec_ids = [f"dec_{uuid.uuid4().hex[:8]}" for _ in range(4)]
    mongo.decision_contexts.insert_many([
        {"user_id": uid, "decision_id": dec_ids[i], "at": now,
         "symbol": "XAUUSD"} for i in range(4)])
    # meta_decisions: 2 TRADE, 1 REDUCE (mult=0.5), 1 SKIP hard-gated
    mongo.meta_decisions.insert_many([
        {"user_id": uid, "decision_id": dec_ids[0], "at": now,
         "decision": "TRADE", "risk_multiplier": 1.0},
        {"user_id": uid, "decision_id": dec_ids[1], "at": now,
         "decision": "TRADE", "risk_multiplier": 1.0},
        {"user_id": uid, "decision_id": dec_ids[2], "at": now,
         "decision": "REDUCE", "risk_multiplier": 0.5},
        {"user_id": uid, "decision_id": dec_ids[3], "at": now,
         "decision": "SKIP", "risk_multiplier": 0.0,
         "uncertainty_detail": {"hard_gate": True}},
    ])
    # closed trades — winner + loser in TRADE cohort, loser in REDUCE cohort
    mongo.trades.insert_many([
        {"user_id": uid, "decision_id": dec_ids[0], "status": "closed",
         "opened_at": now, "closed_at": now, "symbol": "XAUUSD",
         "action": "BUY", "entry_price": 2000.0, "stop_loss": 1990.0,
         "exit_price": 2020.0, "pnl": 200.0, "alpha_clean": True,
         "scope": "ai"},
        {"user_id": uid, "decision_id": dec_ids[1], "status": "closed",
         "opened_at": now, "closed_at": now, "symbol": "XAUUSD",
         "action": "BUY", "entry_price": 2000.0, "stop_loss": 1990.0,
         "exit_price": 1990.0, "pnl": -100.0, "alpha_clean": True,
         "scope": "ai"},
        {"user_id": uid, "decision_id": dec_ids[2], "status": "closed",
         "opened_at": now, "closed_at": now, "symbol": "XAUUSD",
         "action": "BUY", "entry_price": 2000.0, "stop_loss": 1990.0,
         "exit_price": 1995.0, "pnl": -25.0, "alpha_clean": True,
         "scope": "ai"},
    ])
    yield {"session": s, "id": uid, "dec_ids": dec_ids}
    mongo.users.delete_one({"_id": ObjectId(uid)})
    mongo.trades.delete_many({"user_id": uid})
    mongo.meta_decisions.delete_many({"user_id": uid})
    mongo.decision_contexts.delete_many({"user_id": uid})


class TestSeededReport:
    def test_report_reflects_seeded_data(self, seeded_user):
        r = seeded_user["session"].get(
            f"{BASE_URL}/api/brain/report?days=30", timeout=30)
        assert r.status_code == 200, r.text
        body = r.json()
        # funnel counts
        assert body["funnel"]["decisions_minted"] >= 4
        meta = body["funnel"]["meta"]
        assert meta["TRADE"] >= 2
        assert meta["REDUCE"] >= 1
        assert meta["SKIP"] >= 1
        # outcomes
        outcomes = body["outcomes"]
        assert outcomes["closed"] >= 3
        assert outcomes["win_rate"] is not None
        # cannot leak into other users — validate user_id
        assert body["user_id"] == seeded_user["id"]

    def test_interventions_reflects_seeded_data(self, seeded_user):
        r = seeded_user["session"].get(
            f"{BASE_URL}/api/brain/interventions?days=30", timeout=20)
        assert r.status_code == 200, r.text
        body = r.json()
        counts = body["meta"]["counts"]
        assert counts["TRADE"] >= 2
        assert counts["REDUCE"] >= 1
        assert counts["SKIP"] >= 1
        assert body["meta"]["hard_gates"] >= 1
        # cohorts — TRADE has n>=2, REDUCE has n>=1
        assert body["reduce_effect"]["trade_cohort"]["n"] >= 2
        assert body["reduce_effect"]["reduce_cohort"]["n"] >= 1
        # loser downsized by 0.5 → saved_on_losers should be > 0
        assert body["reduce_effect"]["saved_on_losers_usd"] > 0

    def test_tenant_isolation_report(self, seeded_user, user_a):
        """User A must not see user C's seeded data."""
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/report?days=30", timeout=30)
        assert r.status_code == 200
        body = r.json()
        assert body["user_id"] == user_a["id"]
        # a freshly-registered user cannot have any of user C's decisions
        assert body["funnel"]["decisions_minted"] == 0
        assert body["funnel"]["meta"] == {"TRADE": 0, "REDUCE": 0,
                                          "SKIP": 0}
        assert body["outcomes"]["closed"] == 0


# ────────── source-level checks (kept in the http suite of this file) ────


class TestRouterDisabledSemantics:
    def test_router_disabled_multiplier_zero(self):
        from strategy_router import HEALTH_ROUTER_MULT
        assert HEALTH_ROUTER_MULT.get("DISABLED") == 0.0
