"""iter-212 — HTTP end-to-end tests for the NEW/CHANGED endpoints
introduced by the 10 Production-Proof hardening items.

Covers:
  • GET /api/brain/coverage — overall + segments
  • GET /api/brain/value-ledger — observed / estimated / unobservable
  • GET /api/certification/strategy — 4 checks
  • GET /api/certification/system — 6 checks + BOLA
  • POST /api/ops/soak/start | checkpoint | incident   (admin + CSRF)
  • GET  /api/ops/soak/status                          (admin)
  • GET  /api/ops/broker-validation                    (admin)
  • Non-admin 403 on all /api/ops/soak/* and /broker-validation
  • Heartbeat clock telemetry (client_time_ms → agent_clock)
  • /api/brain/health scope=account includes clock_telemetry
  • Regression spot-check on iter-211 endpoints
"""
import os
import time
import uuid
from datetime import datetime, timezone

import pytest
import requests
from bson import ObjectId
from pymongo import MongoClient

pytestmark = pytest.mark.http

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
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
                     json={"email": email, "password": password}, timeout=15)
    assert r.status_code == 200, f"login failed {r.status_code}: {r.text}"
    return r.json()


def _csrf_headers(session: requests.Session) -> dict:
    tok = session.cookies.get("csrf_token")
    assert tok, "csrf_token cookie not set after login"
    return {"X-CSRF-Token": tok}


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    _login(s, ADMIN_EMAIL, ADMIN_PASSWORD)
    return s


def _make_verified_user(mongo) -> tuple[str, str, str]:
    email = f"test_iter212_{uuid.uuid4().hex[:10]}@example.com"
    password = f"Iter212-{uuid.uuid4().hex[:12]}-Zx"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": password,
                            "name": "Iter212 Tester",
                            "terms_agreed": True}, timeout=15)
    assert r.status_code in (200, 201), f"register: {r.status_code} {r.text}"
    mongo.users.update_one({"email": email},
                           {"$set": {"email_verified": True}})
    user_doc = mongo.users.find_one({"email": email})
    return email, password, str(user_doc["_id"])


@pytest.fixture(scope="module")
def user_a(mongo):
    email, password, uid = _make_verified_user(mongo)
    s = requests.Session()
    _login(s, email, password)
    yield {"session": s, "email": email, "password": password, "id": uid}
    mongo.users.delete_one({"_id": ObjectId(uid)})
    mongo.accounts.delete_many({"user_id": uid})


@pytest.fixture(scope="module")
def user_b(mongo):
    email, password, uid = _make_verified_user(mongo)
    s = requests.Session()
    _login(s, email, password)
    yield {"session": s, "email": email, "password": password, "id": uid}
    mongo.users.delete_one({"_id": ObjectId(uid)})
    mongo.accounts.delete_many({"user_id": uid})


@pytest.fixture(scope="module")
def user_a_account(mongo, user_a):
    acc_id = ObjectId()
    mongo.accounts.insert_one({
        "_id": acc_id, "user_id": user_a["id"],
        "broker": "TestBrokerA212", "server": "TestBrokerA212-Demo",
        "mode": "paper", "equity": 10000.0,
        "bridge_token": f"bt_212a_{uuid.uuid4().hex}",
        "last_heartbeat": "2999-01-01T00:00:00+00:00"})
    yield str(acc_id)
    mongo.accounts.delete_one({"_id": acc_id})


@pytest.fixture(scope="module")
def user_b_account(mongo, user_b):
    acc_id = ObjectId()
    mongo.accounts.insert_one({
        "_id": acc_id, "user_id": user_b["id"],
        "broker": "TestBrokerB212", "server": "TestBrokerB212-Demo",
        "mode": "paper", "equity": 10000.0,
        "bridge_token": f"bt_212b_{uuid.uuid4().hex}",
        "last_heartbeat": "2999-01-01T00:00:00+00:00"})
    yield str(acc_id)
    mongo.accounts.delete_one({"_id": acc_id})


# ────────────────────── /api/brain/coverage ─────────────────────────────


class TestBrainCoverageSegments:
    def test_overall_and_segments_present(self, user_a):
        r = user_a["session"].get(f"{BASE_URL}/api/brain/coverage",
                                  timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ("evaluated", "coverage", "ok", "segments"):
            assert k in body, f"missing {k}"
        segs = body["segments"]
        assert isinstance(segs, dict)
        for seg_key in ("by_strategy", "by_symbol",
                        "by_session", "by_regime"):
            assert seg_key in segs, f"segments missing {seg_key}"
            assert isinstance(segs[seg_key], list)
            for row in segs[seg_key]:
                assert "segment" in row
                assert "n" in row
                assert "ok" in row
                # coverage may be None when insufficient
                assert "coverage" in row

    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/brain/coverage", timeout=10)
        assert r.status_code == 401


# ────────────────────── /api/brain/value-ledger ─────────────────────────


class TestValueLedger:
    def test_shape(self, user_a):
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/value-ledger?days=30", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ("days", "entries", "observed_total_usd", "policy", "at"):
            assert k in body, f"missing {k}"
        assert body["days"] == 30
        assert isinstance(body["entries"], list)
        assert len(body["entries"]) >= 1
        allowed_effects = {"observed", "estimated", "unobservable"}
        for e in body["entries"]:
            assert e["effect"] in allowed_effects
            # unobservable entries must NOT carry value_usd
            if e["effect"] == "unobservable":
                assert "value_usd" not in e, (
                    f"unobservable carries value_usd: {e}")
        assert isinstance(body["observed_total_usd"], (int, float))

    def test_days_zero_422(self, user_a):
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/value-ledger?days=0", timeout=10)
        assert r.status_code == 422

    def test_days_91_422(self, user_a):
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/value-ledger?days=91", timeout=10)
        assert r.status_code == 422

    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/brain/value-ledger", timeout=10)
        assert r.status_code == 401


# ────────────────────── /api/certification/strategy ─────────────────────


class TestStrategyCertification:
    def test_shape(self, user_a):
        r = user_a["session"].get(
            f"{BASE_URL}/api/certification/strategy?scope=ai", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kind"] == "strategy"
        assert body["scope"] == "ai"
        assert isinstance(body["passed"], bool)
        keys = {c["key"] for c in body["checks"]}
        assert keys >= {"sample_size", "positive_edge",
                        "coverage_honest", "decay_state"}

    def test_requires_auth(self):
        r = requests.get(
            f"{BASE_URL}/api/certification/strategy?scope=ai", timeout=10)
        assert r.status_code == 401


# ────────────────────── /api/certification/system ───────────────────────


class TestSystemCertification:
    def test_shape_own_account(self, user_a, user_a_account):
        r = user_a["session"].get(
            f"{BASE_URL}/api/certification/system"
            f"?account_id={user_a_account}", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kind"] == "system"
        assert body["account_id"] == user_a_account
        assert isinstance(body["passed"], bool)
        keys = {c["key"] for c in body["checks"]}
        # 6 infra checks
        assert keys >= {"bridge_paired", "heartbeat_fresh",
                        "identity_verified", "clock_health",
                        "latency_evidence", "spread_feed"}
        assert len(body["checks"]) >= 6

    def test_bola_other_users_account_404(self, user_a, user_b_account):
        r = user_a["session"].get(
            f"{BASE_URL}/api/certification/system"
            f"?account_id={user_b_account}", timeout=10)
        assert r.status_code == 404, (
            f"BOLA leak: got {r.status_code} for user B's acc; {r.text}")

    def test_invalid_object_id_404(self, user_a):
        r = user_a["session"].get(
            f"{BASE_URL}/api/certification/system"
            f"?account_id=not-a-real-id", timeout=10)
        assert r.status_code == 404

    def test_requires_auth(self, user_a_account):
        r = requests.get(
            f"{BASE_URL}/api/certification/system"
            f"?account_id={user_a_account}", timeout=10)
        assert r.status_code == 401


# ────────────────── Soak campaign lifecycle (admin) ─────────────────────


@pytest.fixture(scope="module")
def soak_cleanup(mongo):
    """Run before + after: wipe test campaigns / checkpoints / incidents
    so preview leaves the DB clean."""
    # Pre-clean any leftover RUNNING campaign so we can start ours
    mongo.soak_campaigns.delete_many({"status": "RUNNING"})
    ids_seen: list[str] = []
    yield ids_seen
    if ids_seen:
        mongo.soak_campaigns.delete_many({"campaign_id": {"$in": ids_seen}})
        mongo.soak_checkpoints.delete_many({"campaign_id": {"$in": ids_seen}})
        mongo.soak_incidents.delete_many({"campaign_id": {"$in": ids_seen}})
    mongo.soak_campaigns.delete_many({"status": "RUNNING"})


class TestSoakLifecycle:
    def test_start_admin(self, admin_session, soak_cleanup):
        r = admin_session.post(
            f"{BASE_URL}/api/ops/soak/start",
            json={"days": 14, "note": "iter212 http test"},
            headers=_csrf_headers(admin_session), timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "campaign_id" in body, body
        assert body["status"] == "RUNNING"
        assert body["days"] == 14
        soak_cleanup.append(body["campaign_id"])

    def test_start_already_running(self, admin_session, soak_cleanup):
        r = admin_session.post(
            f"{BASE_URL}/api/ops/soak/start",
            json={"days": 14},
            headers=_csrf_headers(admin_session), timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("error") == "campaign_already_running"

    def test_checkpoint(self, admin_session):
        r = admin_session.post(
            f"{BASE_URL}/api/ops/soak/checkpoint",
            headers=_csrf_headers(admin_session), timeout=20)
        assert r.status_code == 200, r.text
        cp = r.json()
        assert "day" in cp
        assert cp["day"] == 1
        assert "green" in cp
        assert isinstance(cp["green"], bool)

    def test_incident_minor(self, admin_session):
        r = admin_session.post(
            f"{BASE_URL}/api/ops/soak/incident",
            json={"severity": "minor", "note": "x"},
            headers=_csrf_headers(admin_session), timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["severity"] == "minor"
        assert body["note"] == "x"

    def test_incident_bogus_severity_400(self, admin_session):
        r = admin_session.post(
            f"{BASE_URL}/api/ops/soak/incident",
            json={"severity": "bogus", "note": "x"},
            headers=_csrf_headers(admin_session), timeout=15)
        assert r.status_code == 400

    def test_status(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/ops/soak/status", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ("campaign", "evaluation", "checkpoints", "incidents"):
            assert k in body, f"missing {k}: keys={list(body.keys())}"
        assert body["evaluation"]["verdict"] == "RUNNING"
        assert "criteria" in body["evaluation"]
        assert len(body["checkpoints"]) >= 1
        assert len(body["incidents"]) >= 1


# ───────────── Non-admin 403 on ALL soak + broker-validation ────────────


class TestSoakAuthzNonAdmin:
    def test_start_forbidden(self, user_a):
        r = user_a["session"].post(
            f"{BASE_URL}/api/ops/soak/start",
            json={"days": 14},
            headers=_csrf_headers(user_a["session"]), timeout=10)
        assert r.status_code == 403, f"expected 403, got {r.status_code}"

    def test_checkpoint_forbidden(self, user_a):
        r = user_a["session"].post(
            f"{BASE_URL}/api/ops/soak/checkpoint",
            headers=_csrf_headers(user_a["session"]), timeout=10)
        assert r.status_code == 403

    def test_incident_forbidden(self, user_a):
        r = user_a["session"].post(
            f"{BASE_URL}/api/ops/soak/incident",
            json={"severity": "minor", "note": "x"},
            headers=_csrf_headers(user_a["session"]), timeout=10)
        assert r.status_code == 403

    def test_status_forbidden(self, user_a):
        r = user_a["session"].get(
            f"{BASE_URL}/api/ops/soak/status", timeout=10)
        assert r.status_code == 403

    def test_broker_validation_forbidden(self, user_a, user_a_account):
        r = user_a["session"].get(
            f"{BASE_URL}/api/ops/broker-validation"
            f"?account_id={user_a_account}", timeout=10)
        assert r.status_code == 403


class TestBrokerValidationAdmin:
    def test_admin_checklist(self, admin_session, user_a_account):
        r = admin_session.get(
            f"{BASE_URL}/api/ops/broker-validation"
            f"?account_id={user_a_account}", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["account_id"] == user_a_account
        assert isinstance(body["passed"], bool)
        assert isinstance(body["checks"], list)
        assert len(body["checks"]) == 7
        keys = {c["key"] for c in body["checks"]}
        assert keys == {"live_environment", "identity_verified",
                        "heartbeat_live", "deal_history_synced",
                        "round_trip_trade", "latency_traced",
                        "clock_health"}

    def test_admin_bad_id_404(self, admin_session):
        r = admin_session.get(
            f"{BASE_URL}/api/ops/broker-validation"
            f"?account_id={ObjectId()}", timeout=10)
        assert r.status_code == 404


# ───────────────── Heartbeat clock telemetry (end-to-end) ───────────────


@pytest.fixture(scope="module")
def hb_account(mongo, user_a):
    acc_id = ObjectId()
    token = f"bt_212hb_{uuid.uuid4().hex}"
    mongo.accounts.insert_one({
        "_id": acc_id, "user_id": user_a["id"],
        "broker": "TestHBBroker212", "server": "TestHB-Demo",
        "mode": "paper", "equity": 10000.0,
        "bridge_token": token,
        "last_heartbeat": None})
    yield {"id": str(acc_id), "token": token}
    mongo.accounts.delete_one({"_id": acc_id})


class TestHeartbeatClockTelemetry:
    def test_ok_when_synced(self, mongo, hb_account):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        r = requests.post(f"{BASE_URL}/api/bridge/heartbeat", json={
            "bridge_token": hb_account["token"],
            "balance": 10000.0, "equity": 10000.0,
            "open_positions": 0,
            "client_time_ms": now_ms, "ntp_synced": True}, timeout=15)
        assert r.status_code == 200, r.text
        doc = mongo.accounts.find_one({"_id": ObjectId(hb_account["id"])})
        ac = doc.get("agent_clock")
        assert ac is not None, f"agent_clock not stamped: {doc.keys()}"
        assert ac["status"] == "OK", ac
        assert abs(ac["skew_ms"]) <= 1500

    def test_skew_suspected_when_far_past(self, mongo, hb_account):
        past_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 10_000
        r = requests.post(f"{BASE_URL}/api/bridge/heartbeat", json={
            "bridge_token": hb_account["token"],
            "balance": 10000.0, "equity": 10000.0,
            "open_positions": 0,
            "client_time_ms": past_ms, "ntp_synced": True}, timeout=15)
        assert r.status_code == 200, r.text
        doc = mongo.accounts.find_one({"_id": ObjectId(hb_account["id"])})
        ac = doc["agent_clock"]
        assert ac["status"] == "SKEW_SUSPECTED", ac

    def test_clock_skew_endpoint_shows_reported(self, user_a, hb_account):
        r = user_a["session"].get(
            f"{BASE_URL}/api/latency/clock-skew", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        rows = {row["account_id"]: row for row in body["accounts"]}
        assert hb_account["id"] in rows, (
            f"hb account not surfaced: {list(rows.keys())}")
        row = rows[hb_account["id"]]
        assert row.get("reported") is not None
        assert hb_account["id"] in body["suspected"]


# ─────────────── /api/brain/health scope=account clock_telemetry ────────


class TestBrainHealthClockTelemetry:
    def test_field_present(self, user_a, hb_account):
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/health?scope=account"
            f"&account_id={hb_account['id']}", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        # iter-212: clock_telemetry field exposed (either the agent_clock
        # dict, or a NO_DATA sentinel note)
        assert "clock_telemetry" in body, (
            f"clock_telemetry missing; keys={list(body.keys())}")

    def test_field_present_for_new_account_no_data(self, user_a,
                                                   user_a_account):
        # user_a_account has never posted a heartbeat with client_time_ms
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/health?scope=account"
            f"&account_id={user_a_account}", timeout=15)
        assert r.status_code == 200, r.text
        assert "clock_telemetry" in r.json()


# ──────────────── Regression spot-checks (iter-211) ─────────────────────


class TestRegressionSpotCheck:
    def test_brain_report_ok(self, user_a):
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/report?days=30", timeout=20)
        assert r.status_code == 200

    def test_brain_interventions_ok(self, user_a):
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/interventions?days=30", timeout=20)
        assert r.status_code == 200

    def test_brain_health_global_ok(self, user_a):
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/health?scope=global", timeout=15)
        assert r.status_code == 200

    def test_brain_health_regional_ok(self, user_a):
        r = user_a["session"].get(
            f"{BASE_URL}/api/brain/health?scope=regional", timeout=15)
        assert r.status_code == 200

    def test_latency_summary_ok(self, admin_session):
        r = admin_session.get(
            f"{BASE_URL}/api/latency/summary?days=7", timeout=15)
        assert r.status_code == 200
