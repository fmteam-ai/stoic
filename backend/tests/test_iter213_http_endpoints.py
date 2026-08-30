"""iter-213 — HTTP end-to-end tests for the P0/P1 hardening pass.

Covers:
  * Soak lifecycle hardened (frozen_versions, invariants, scope,
    global_health, version_drift, green, evidence chain, severity
    definitions, coverage_required=1.0, major_incidents_within_budget)
  * GET /api/ops/soak/evidence (admin-only, hash-chain valid=true)
  * Account-scoped soak invariants only count campaign account trades
  * GET /api/certification/strategy?scope=ai adds tier + 5 checks
  * POST /api/certification/issue strategy 30d / system 7d / BOLA 404
    / bad kind 400
  * GET /api/certification/active tenant isolation + admin sees all
  * POST /api/certification/revoke admin-only, revokes cert, 404 unknown
  * GET /api/ops/broker-validation live_environment first check
    (LIVE/DEMO/PAPER)
  * GET /api/brain/health scope=account exposes broker_environment
"""
import os
import uuid
from datetime import datetime, timezone

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


def _login(session: requests.Session, email: str, password: str):
    r = session.post(f"{BASE_URL}/api/auth/login",
                     json={"email": email, "password": password}, timeout=15)
    assert r.status_code == 200, f"login failed {r.status_code}: {r.text}"
    return r.json()


def _csrf(session: requests.Session) -> dict:
    tok = session.cookies.get("csrf_token")
    assert tok, "csrf_token cookie not set after login"
    return {"X-CSRF-Token": tok}


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    _login(s, ADMIN_EMAIL, ADMIN_PASSWORD)
    return s


def _make_user(mongo):
    email = f"test_iter213_{uuid.uuid4().hex[:10]}@example.com"
    password = f"Iter213-{uuid.uuid4().hex[:12]}-Zx"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": password,
                            "name": "Iter213 Tester",
                            "terms_agreed": True}, timeout=15)
    assert r.status_code in (200, 201), f"register {r.status_code}: {r.text}"
    mongo.users.update_one({"email": email},
                           {"$set": {"email_verified": True}})
    doc = mongo.users.find_one({"email": email})
    s = requests.Session()
    _login(s, email, password)
    return {"session": s, "email": email, "id": str(doc["_id"])}


@pytest.fixture(scope="module")
def user_a(mongo):
    u = _make_user(mongo)
    yield u
    mongo.users.delete_one({"_id": ObjectId(u["id"])})
    mongo.accounts.delete_many({"user_id": u["id"]})
    mongo.trades.delete_many({"account_id": {"$regex": "^iter213_"}})
    mongo.certifications.delete_many({"user_id": u["id"]})


@pytest.fixture(scope="module")
def user_b(mongo):
    u = _make_user(mongo)
    yield u
    mongo.users.delete_one({"_id": ObjectId(u["id"])})
    mongo.accounts.delete_many({"user_id": u["id"]})
    mongo.certifications.delete_many({"user_id": u["id"]})


def _mk_account(mongo, user_id, *, mode="paper", server="ICMarkets-Real3",
                broker_env=None, extra=None):
    acc_id = ObjectId()
    doc = {"_id": acc_id, "user_id": user_id,
           "broker": "TestBroker213",
           "broker_server": server,
           "server": server,
           "mode": mode,
           "equity": 10000.0,
           "bridge_token": f"bt_213_{uuid.uuid4().hex}",
           "last_heartbeat": datetime.now(timezone.utc).isoformat()}
    if broker_env:
        doc["broker_environment"] = broker_env
    if extra:
        doc.update(extra)
    mongo.accounts.insert_one(doc)
    return str(acc_id)


# ───────────────── cleanup fixture (RUNNING campaign safety) ────────────


@pytest.fixture(scope="module")
def soak_cleanup(mongo):
    mongo.soak_campaigns.delete_many({"status": "RUNNING"})
    ids: list[str] = []
    yield ids
    if ids:
        mongo.soak_campaigns.delete_many({"campaign_id": {"$in": ids}})
        mongo.soak_checkpoints.delete_many({"campaign_id": {"$in": ids}})
        mongo.soak_incidents.delete_many({"campaign_id": {"$in": ids}})
        mongo.production_evidence.delete_many({"campaign_id": {"$in": ids}})
    mongo.soak_campaigns.delete_many({"status": "RUNNING"})


# ─────────────────────── Soak lifecycle (hardened) ──────────────────────


class TestSoakLifecycleHardened:
    def test_start_has_frozen_versions(self, admin_session, soak_cleanup):
        r = admin_session.post(
            f"{BASE_URL}/api/ops/soak/start",
            json={"days": 14, "note": "iter213 http"},
            headers=_csrf(admin_session), timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "campaign_id" in body and body["status"] == "RUNNING"
        assert "frozen_versions" in body, body
        fv = body["frozen_versions"]
        assert "ea_version" in fv and "release" in fv
        assert isinstance(fv["release"], str) and len(fv["release"]) >= 10
        soak_cleanup.append(body["campaign_id"])

    def test_checkpoint_full_shape(self, admin_session, soak_cleanup):
        r = admin_session.post(
            f"{BASE_URL}/api/ops/soak/checkpoint",
            headers=_csrf(admin_session), timeout=25)
        assert r.status_code == 200, r.text
        cp = r.json()
        for k in ("scope", "account_connected", "invariants",
                  "global_health", "version_drift", "green", "evidence"):
            assert k in cp, f"missing key {k}: {list(cp.keys())}"
        inv = cp["invariants"]
        for k in ("unknown_rate", "unknown_rate_ok",
                  "duplicate_executions", "duplicates_ok",
                  "unconfirmed_ghosts", "reconciliation_ok",
                  "rejects_24h"):
            assert k in inv, f"invariant missing {k}"
        gh = cp["global_health"]
        for k in ("degraded_mode", "critical_failing"):
            assert k in gh, f"global_health missing {k}"
        ev = cp["evidence"]
        for k in ("seq", "hash", "prev_hash", "release", "checkpoint"):
            assert k in ev, f"evidence missing {k}"
        assert isinstance(cp["green"], bool)
        assert isinstance(cp["version_drift"], list)

    def test_status_severity_and_criteria(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/ops/soak/status", timeout=15)
        assert r.status_code == 200, r.text
        b = r.json()
        assert "severity_definitions" in b
        assert set(b["severity_definitions"].keys()) == {
            "critical", "major", "minor"}
        ev = b["evaluation"]
        assert ev["coverage_required"] == 1.0
        assert "major_incidents_within_budget" in ev["criteria"]

    def test_evidence_admin_chain_valid(self, admin_session, soak_cleanup):
        r = admin_session.get(f"{BASE_URL}/api/ops/soak/evidence",
                              timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ("records", "count", "chain_valid"):
            assert k in body, body
        assert body["chain_valid"] is True
        assert body["count"] >= 1
        rec = body["records"][0]
        for k in ("seq", "prev_hash", "hash", "release", "checkpoint"):
            assert k in rec, rec

    def test_evidence_non_admin_forbidden(self, user_a):
        r = user_a["session"].get(f"{BASE_URL}/api/ops/soak/evidence",
                                  timeout=10)
        assert r.status_code == 403


# ────────── Account-scoped soak: invariants count only campaign acct ─────


class TestAccountScopedSoak:
    def test_scoped_invariants_only_count_campaign_account(
            self, admin_session, mongo, user_a):
        # Pre-clean any leftover RUNNING campaign from previous class
        mongo.soak_campaigns.delete_many({"status": "RUNNING"})
        # Seed two accounts + trades on both
        acc_campaign = _mk_account(mongo, user_a["id"],
                                   broker_env="LIVE",
                                   server="ICMarkets-Real3")
        acc_other = _mk_account(mongo, user_a["id"],
                                broker_env="LIVE",
                                server="ICMarkets-Real4")
        now_iso = datetime.now(timezone.utc).isoformat()
        # 4 trades on campaign account (with latency trace so unknown_rate=0)
        mongo.trades.insert_many([
            {"account_id": acc_campaign, "opened_at": now_iso,
             "status": "closed", "signal_id": f"iter213_sig_{i}",
             "mt5_ticket": 900000 + i,
             "latency_trace": {"t9_ms": 42}}
            for i in range(4)])
        # 2 trades on other account with DUPLICATE signal_id (must NOT
        # count into campaign checkpoint invariants)
        mongo.trades.insert_many([
            {"account_id": acc_other, "opened_at": now_iso,
             "status": "closed", "signal_id": "iter213_dup_shared",
             "mt5_ticket": 800001,
             "latency_trace": {"t9_ms": 42}},
            {"account_id": acc_other, "opened_at": now_iso,
             "status": "closed", "signal_id": "iter213_dup_shared",
             "mt5_ticket": 800002,
             "latency_trace": {"t9_ms": 42}},
        ])
        # start scoped campaign
        r = admin_session.post(
            f"{BASE_URL}/api/ops/soak/start",
            json={"days": 14, "account_id": acc_campaign,
                  "note": "iter213 scoped"},
            headers=_csrf(admin_session), timeout=15)
        assert r.status_code == 200, r.text
        camp = r.json()
        assert camp.get("status") == "RUNNING", camp
        try:
            cp_r = admin_session.post(
                f"{BASE_URL}/api/ops/soak/checkpoint",
                headers=_csrf(admin_session), timeout=25)
            assert cp_r.status_code == 200, cp_r.text
            cp = cp_r.json()
            assert cp["scope"]["account_id"] == acc_campaign
            inv = cp["invariants"]
            assert inv["trades_24h"] == 4, (
                f"expected only 4 campaign-account trades, "
                f"got {inv['trades_24h']}: {inv}")
            assert inv["duplicate_executions"] == 0, (
                f"duplicates leaked from other account: {inv}")
            assert inv["duplicates_ok"] is True
            assert inv["unknown_rate"] == 0.0
            assert inv["unknown_rate_ok"] is True
        finally:
            # cleanup this campaign
            cid = camp["campaign_id"]
            mongo.soak_campaigns.delete_many({"campaign_id": cid})
            mongo.soak_checkpoints.delete_many({"campaign_id": cid})
            mongo.soak_incidents.delete_many({"campaign_id": cid})
            mongo.production_evidence.delete_many({"campaign_id": cid})
            mongo.trades.delete_many({
                "account_id": {"$in": [acc_campaign, acc_other]}})
            mongo.accounts.delete_many({
                "_id": {"$in": [ObjectId(acc_campaign),
                                ObjectId(acc_other)]}})


# ───────────────────── Strategy certification tier ──────────────────────


class TestStrategyTier:
    def test_tier_and_five_checks(self, user_a):
        r = user_a["session"].get(
            f"{BASE_URL}/api/certification/strategy?scope=ai", timeout=15)
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["kind"] == "strategy"
        assert b["scope"] == "ai"
        assert b["tier"] in ("CERTIFIED_A", "CERTIFIED_B",
                             "PROVISIONAL", "UNCERTIFIED")
        keys = {c["key"] for c in b["checks"]}
        assert keys >= {"sample_size", "positive_edge",
                        "lower_bound_clears", "coverage_honest",
                        "decay_state"}
        # passed only if tier A/B
        assert b["passed"] == (b["tier"] in ("CERTIFIED_A", "CERTIFIED_B"))


# ───────────────────────── /certification/issue ─────────────────────────


class TestCertificationIssue:
    def test_issue_strategy_persists_with_expiry_30d(self, user_a, mongo):
        r = user_a["session"].post(
            f"{BASE_URL}/api/certification/issue",
            json={"kind": "strategy", "scope": "ai"},
            headers=_csrf(user_a["session"]), timeout=15)
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["cert_id"].startswith("cert_")
        assert b["kind"] == "strategy"
        assert "tier" in b
        assert "expires_at" in b
        exp = datetime.fromisoformat(b["expires_at"])
        iss = datetime.fromisoformat(b["issued_at"])
        delta_days = (exp - iss).total_seconds() / 86400
        assert 29.9 < delta_days < 30.1, delta_days
        # persisted
        doc = mongo.certifications.find_one({"cert_id": b["cert_id"]})
        assert doc is not None

    def test_issue_system_own_account_7d(self, user_a, mongo):
        acc = _mk_account(mongo, user_a["id"], mode="paper",
                          server="ICMarkets-Real1")
        try:
            r = user_a["session"].post(
                f"{BASE_URL}/api/certification/issue",
                json={"kind": "system", "account_id": acc},
                headers=_csrf(user_a["session"]), timeout=15)
            assert r.status_code == 200, r.text
            b = r.json()
            assert b["kind"] == "system"
            exp = datetime.fromisoformat(b["expires_at"])
            iss = datetime.fromisoformat(b["issued_at"])
            days = (exp - iss).total_seconds() / 86400
            assert 6.9 < days < 7.1, days
        finally:
            mongo.accounts.delete_one({"_id": ObjectId(acc)})

    def test_issue_system_bola_404(self, user_a, user_b, mongo):
        # user_b owns account, user_a tries to cert it
        other_acc = _mk_account(mongo, user_b["id"], mode="paper",
                                server="ICMarkets-Real2")
        try:
            r = user_a["session"].post(
                f"{BASE_URL}/api/certification/issue",
                json={"kind": "system", "account_id": other_acc},
                headers=_csrf(user_a["session"]), timeout=10)
            assert r.status_code == 404, (
                f"BOLA leak: got {r.status_code}: {r.text}")
        finally:
            mongo.accounts.delete_one({"_id": ObjectId(other_acc)})

    def test_issue_bad_kind_400(self, user_a):
        r = user_a["session"].post(
            f"{BASE_URL}/api/certification/issue",
            json={"kind": "nonsense"},
            headers=_csrf(user_a["session"]), timeout=10)
        assert r.status_code == 400


# ─────────────────────── /certification/active ──────────────────────────


class TestCertificationActive:
    def test_user_isolation(self, user_a, user_b):
        # user_b creates a cert of their own
        r_b = user_b["session"].post(
            f"{BASE_URL}/api/certification/issue",
            json={"kind": "strategy", "scope": "ai"},
            headers=_csrf(user_b["session"]), timeout=15)
        assert r_b.status_code == 200
        cert_b = r_b.json()["cert_id"]
        # user_a fetches active — must NOT contain user_b's cert
        r_a = user_a["session"].get(
            f"{BASE_URL}/api/certification/active", timeout=10)
        assert r_a.status_code == 200
        ids_a = {c["cert_id"] for c in r_a.json()["certifications"]}
        assert cert_b not in ids_a, "tenant leak in /active"
        # each cert has validity fields
        for c in r_a.json()["certifications"]:
            assert "valid" in c
            assert "expired" in c
            assert "revoked" in c

    def test_admin_sees_all(self, admin_session, user_a):
        # ensure user_a has a cert
        r = user_a["session"].post(
            f"{BASE_URL}/api/certification/issue",
            json={"kind": "strategy", "scope": "ai"},
            headers=_csrf(user_a["session"]), timeout=15)
        assert r.status_code == 200
        cid = r.json()["cert_id"]
        # admin lists — must include it
        r_admin = admin_session.get(
            f"{BASE_URL}/api/certification/active", timeout=15)
        assert r_admin.status_code == 200
        ids = {c["cert_id"] for c in r_admin.json()["certifications"]}
        assert cid in ids, "admin cannot see user cert"


# ─────────────────────── /certification/revoke ──────────────────────────


class TestCertificationRevoke:
    def test_revoke_admin_only_and_reflected(self, admin_session,
                                             user_a, mongo):
        r = user_a["session"].post(
            f"{BASE_URL}/api/certification/issue",
            json={"kind": "strategy", "scope": "ai"},
            headers=_csrf(user_a["session"]), timeout=15)
        assert r.status_code == 200
        cid = r.json()["cert_id"]
        # non-admin revoke → 403
        r_nonadmin = user_a["session"].post(
            f"{BASE_URL}/api/certification/revoke",
            json={"cert_id": cid, "reason": "test"},
            headers=_csrf(user_a["session"]), timeout=10)
        assert r_nonadmin.status_code == 403
        # admin revoke → 200
        r_ok = admin_session.post(
            f"{BASE_URL}/api/certification/revoke",
            json={"cert_id": cid, "reason": "iter213 test"},
            headers=_csrf(admin_session), timeout=10)
        assert r_ok.status_code == 200, r_ok.text
        # /active now shows valid:false for that cert
        r_active = user_a["session"].get(
            f"{BASE_URL}/api/certification/active", timeout=10)
        assert r_active.status_code == 200
        rec = next((c for c in r_active.json()["certifications"]
                    if c["cert_id"] == cid), None)
        assert rec is not None
        assert rec["revoked"] is True
        assert rec["valid"] is False

    def test_revoke_unknown_404(self, admin_session):
        r = admin_session.post(
            f"{BASE_URL}/api/certification/revoke",
            json={"cert_id": "cert_doesnotexist", "reason": "x"},
            headers=_csrf(admin_session), timeout=10)
        assert r.status_code == 404


# ───────────── broker-validation live_environment classification ────────


class TestBrokerValidationLiveEnv:
    def test_demo_detected_from_server_name(self, admin_session,
                                            mongo, user_a):
        acc = _mk_account(mongo, user_a["id"], mode="live",
                          server="ICMarkets-Demo02")
        try:
            r = admin_session.get(
                f"{BASE_URL}/api/ops/broker-validation"
                f"?account_id={acc}", timeout=15)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body.get("broker_environment") == "DEMO", body
            first = body["checks"][0]
            assert first["key"] == "live_environment", first
            assert first.get("value") == "DEMO"
            assert first["ok"] is False
        finally:
            mongo.accounts.delete_one({"_id": ObjectId(acc)})

    def test_paper_detected_from_mode(self, admin_session, mongo, user_a):
        acc = _mk_account(mongo, user_a["id"], mode="paper",
                          server="ICMarkets-Real9")
        try:
            r = admin_session.get(
                f"{BASE_URL}/api/ops/broker-validation"
                f"?account_id={acc}", timeout=15)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body.get("broker_environment") == "PAPER"
            first = body["checks"][0]
            assert first["key"] == "live_environment"
            assert first["value"] == "PAPER"
        finally:
            mongo.accounts.delete_one({"_id": ObjectId(acc)})

    def test_live_detected(self, admin_session, mongo, user_a):
        acc = _mk_account(mongo, user_a["id"], mode="live",
                          server="Exness-Real7", broker_env="LIVE")
        try:
            r = admin_session.get(
                f"{BASE_URL}/api/ops/broker-validation"
                f"?account_id={acc}", timeout=15)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body.get("broker_environment") == "LIVE"
            first = body["checks"][0]
            assert first["key"] == "live_environment"
            assert first["ok"] is True
        finally:
            mongo.accounts.delete_one({"_id": ObjectId(acc)})


# ─────────── /api/brain/health?scope=account carries broker_environment


class TestBrainHealthBrokerEnvironment:
    def test_field_present(self, user_a, mongo):
        acc = _mk_account(mongo, user_a["id"], mode="paper",
                          server="ICMarkets-Real3")
        try:
            r = user_a["session"].get(
                f"{BASE_URL}/api/brain/health?scope=account"
                f"&account_id={acc}", timeout=15)
            assert r.status_code == 200, r.text
            body = r.json()
            assert "broker_environment" in body, (
                f"broker_environment missing; keys={list(body.keys())}")
            assert body["broker_environment"] in ("LIVE", "DEMO", "PAPER")
            assert body["broker_environment"] == "PAPER"
        finally:
            mongo.accounts.delete_one({"_id": ObjectId(acc)})


# ─────────────── regression spot-check (iter-135/136 endpoints) ─────────


class TestRegressionSpotCheck:
    def test_brain_coverage(self, user_a):
        assert user_a["session"].get(
            f"{BASE_URL}/api/brain/coverage",
            timeout=15).status_code == 200

    def test_brain_value_ledger(self, user_a):
        assert user_a["session"].get(
            f"{BASE_URL}/api/brain/value-ledger?days=30",
            timeout=15).status_code == 200

    def test_latency_clock_skew(self, user_a):
        assert user_a["session"].get(
            f"{BASE_URL}/api/latency/clock-skew",
            timeout=15).status_code == 200
