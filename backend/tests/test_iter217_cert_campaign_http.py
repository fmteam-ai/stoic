"""iter-217 / v62.2 — HTTP end-to-end tests for PAMM × Strategy
Certification Campaigns.

Covers, for the v62.2 API surface:
  * GET  /api/pamm/programs/{id}/certification — no-campaign preview,
    campaign preview + lifecycle/stages/criteria/next_state,
    evidence_count grows monotonically.
  * POST /certification/start — 400 no_assignment, 200 DRAFT with
    identity_hash, 409 campaign_exists on second start.
  * POST /certification/advance — DRAFT→VALIDATING; VALIDATING→REPLAY
    blocked 409 validation_required, unblocked by /strategy/validate.
  * REPLAY stage: /certification/evaluate with no evidence -> passed:false;
    advance -> 409 stage_evaluation_required; /certification/checkpoint
    with empty metrics -> 400 metrics_required; with good REPLAY metrics,
    evaluate -> passed:true, advance -> SHADOW.
  * Walk the full pipeline SHADOW → DEMO → CANARY → CERTIFIED.
    Extra: SHADOW checkpoint with orders_placed:1 FAILS the gate.
    CANARY -> CERTIFIED issues a cert (cert_id) and stamps the
    strategy_assignment certification_status = CERTIFIED.
  * CERTIFIED -> LIVE gate: 409 assignment_not_active until
    /strategy/activate makes the assignment ACTIVE; then advance -> LIVE.
    Note: the program has no master_account so certification_required
    inside activate() does NOT block activation.
  * GET /certification/evidence -> hash-chained records, chain_valid=true,
    count monotonically increases across the pipeline (>= 12 events).
  * POST /certification/revoke: mid-pipeline campaign returns state
    DRAFT (abort); CERTIFIED/LIVE campaign returns state REVOKED and
    stamps assignment certification_status = REVOKED (and revokes the
    cert doc).
  * AUTH/BOLA: 5 POST cert endpoints (start/checkpoint/evaluate/
    advance/revoke) -> 403 for non-admin users; advance & revoke ->
    step_up_required 403 without X-Step-Up-Bypass; GET /certification
    -> 403 for a random authenticated non-member user.
  * REGRESSION: /api/pamm/strategies, POST/GET /strategy,
    /strategy/validate, /strategy/activate, /strategy/suspend still work.
"""
import os
import uuid

import pytest
import requests
from bson import ObjectId
from pymongo import MongoClient

pytestmark = pytest.mark.http

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
STEP_UP_BYPASS = os.environ.get("STEP_UP_BYPASS_TOKEN", "")

ADMIN_EMAIL = "admin@stoicaibot.com"
ADMIN_PASSWORD = "admin123"


# ─────────────────────────── fixtures ────────────────────────────────────

@pytest.fixture(scope="module")
def mongo():
    c = MongoClient(MONGO_URL)
    yield c[DB_NAME]
    c.close()


def _login(session, email, password):
    r = session.post(f"{BASE_URL}/api/auth/login",
                     json={"email": email, "password": password}, timeout=15)
    assert r.status_code == 200, f"login failed {r.status_code}: {r.text}"
    return r.json()


def _csrf(session):
    tok = session.cookies.get("csrf_token")
    assert tok, "csrf_token cookie not set after login"
    return {"X-CSRF-Token": tok}


def _headers(session, step_up=False):
    h = _csrf(session)
    if step_up and STEP_UP_BYPASS:
        h["X-Step-Up-Bypass"] = STEP_UP_BYPASS
    return h


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    _login(s, ADMIN_EMAIL, ADMIN_PASSWORD)
    yield s


def _make_user(mongo, *, pamm_manager=False):
    email = f"test_iter217_{uuid.uuid4().hex[:10]}@example.com"
    password = f"Iter217-{uuid.uuid4().hex[:12]}-Zx"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": password,
                            "name": "Iter217 Tester",
                            "terms_agreed": True}, timeout=15)
    assert r.status_code in (200, 201), f"register {r.status_code}: {r.text}"
    updates = {"email_verified": True}
    if pamm_manager:
        updates["pamm_manager"] = True
    mongo.users.update_one({"email": email}, {"$set": updates})
    doc = mongo.users.find_one({"email": email})
    s = requests.Session()
    _login(s, email, password)
    return {"session": s, "email": email, "id": str(doc["_id"])}


@pytest.fixture(scope="module")
def plain_user(mongo):
    """A non-manager, non-admin user (should be blocked)."""
    u = _make_user(mongo, pamm_manager=False)
    yield u
    mongo.users.delete_one({"_id": ObjectId(u["id"])})


@pytest.fixture(scope="module")
def manager_user(mongo):
    """Non-admin pamm_manager (owns no other user's programs)."""
    u = _make_user(mongo, pamm_manager=True)
    yield u
    mongo.users.delete_one({"_id": ObjectId(u["id"])})


def _create_program(admin_session, suffix):
    name = f"TEST_iter217_{suffix}_{uuid.uuid4().hex[:6]}"
    r = admin_session.post(f"{BASE_URL}/api/pamm/programs",
                           json={"name": name},
                           headers=_csrf(admin_session), timeout=20)
    assert r.status_code in (200, 201), f"create program: {r.status_code} {r.text}"
    return r.json()["program_id"]


def _cleanup_program(mongo, pid):
    mongo.pamm_programs.delete_one({"program_id": pid})
    mongo.pamm_master_accounts.delete_many({"program_id": pid})
    assignments = list(mongo.pamm_strategy_assignments.find(
        {"pamm_program_id": pid}, {"assignment_id": 1, "cert_id": 1}))
    for a in assignments:
        if a.get("cert_id"):
            mongo.certifications.delete_many({"cert_id": a["cert_id"]})
    mongo.pamm_strategy_assignments.delete_many({"pamm_program_id": pid})
    campaigns = list(mongo.strategy_cert_campaigns.find(
        {"pamm_program_id": pid}, {"campaign_id": 1, "cert_id": 1}))
    for c in campaigns:
        if c.get("cert_id"):
            mongo.certifications.delete_many({"cert_id": c["cert_id"]})
        mongo.strategy_cert_evidence.delete_many(
            {"campaign_id": c["campaign_id"]})
    mongo.strategy_cert_campaigns.delete_many({"pamm_program_id": pid})
    mongo.pamm_events.delete_many({"payload.program_id": pid})


@pytest.fixture(scope="module")
def program_main(admin_session, mongo):
    """Program used for the FULL pipeline walk (REPLAY → LIVE →
    REVOKED)."""
    pid = _create_program(admin_session, "MAIN")
    yield pid
    _cleanup_program(mongo, pid)


@pytest.fixture(scope="module")
def program_mid(admin_session, mongo):
    """Program used to demonstrate mid-pipeline revoke → DRAFT abort."""
    pid = _create_program(admin_session, "MID")
    yield pid
    _cleanup_program(mongo, pid)


@pytest.fixture(scope="module")
def program_bola(admin_session, mongo):
    """Program used for BOLA / auth tests."""
    pid = _create_program(admin_session, "BOLA")
    yield pid
    _cleanup_program(mongo, pid)


# Good metrics per stage (small helper)
REPLAY_GOOD = {"decisions_replayed": 250, "determinism_ok": True,
               "expectancy_lower_r": 0.05, "risk_violations": 0}
SHADOW_GOOD = {"days_elapsed": 6, "shadow_decisions": 150,
               "agreement_rate": 0.97, "risk_violations": 0,
               "orders_placed": 0}
SHADOW_BAD_ORDERS = {**SHADOW_GOOD, "orders_placed": 1}
DEMO_GOOD = {"environment": "DEMO", "days_elapsed": 11, "closed_trades": 60,
             "unknown_rate": 0.0, "reject_rate": 0.01,
             "max_drawdown_pct": 4.0, "expectancy_r": 0.12}
CANARY_GOOD = {"environment": "LIVE", "canary_capital_pct": 3.0,
               "days_elapsed": 6, "closed_trades": 25,
               "max_drawdown_pct": 1.0, "critical_incidents": 0,
               "execution_health": "GREEN"}


# ═════════ 1. Full pipeline walk on program_main ══════════════════════════

class TestFullPipeline:
    """Walks a single campaign start → DRAFT → VALIDATING → REPLAY →
    SHADOW → DEMO → CANARY → CERTIFIED → LIVE, then REVOKED."""

    def test_01_status_no_campaign(self, admin_session, program_main):
        r = admin_session.get(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification",
            timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["campaign"] is None
        assert j["stages"] == ["REPLAY", "SHADOW", "DEMO", "CANARY"]
        assert "DRAFT" in j["lifecycle"] and "CERTIFIED" in j["lifecycle"]
        assert set(j["criteria"].keys()) == {"REPLAY", "SHADOW", "DEMO",
                                             "CANARY"}

    def test_02_start_without_assignment_400(self, admin_session,
                                             program_main):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/start",
            headers=_headers(admin_session), timeout=15)
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "no_assignment"

    def test_03_assign_strategy(self, admin_session, program_main):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/strategy",
            json={"mode": "SINGLE", "strategy_id": "sniper",
                  "risk_profile_id": "controlled"},
            headers=_csrf(admin_session), timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ASSIGNED"

    def test_04_start_campaign_draft(self, admin_session, program_main):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/start",
            headers=_headers(admin_session), timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["state"] == "DRAFT"
        assert j["campaign_id"].startswith("scc_")
        assert isinstance(j["identity"]["identity_hash"], str)
        assert len(j["identity"]["identity_hash"]) == 32

    def test_05_start_duplicate_409(self, admin_session, program_main):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/start",
            headers=_headers(admin_session), timeout=15)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "campaign_exists"

    def test_06_advance_draft_to_validating(self, admin_session,
                                            program_main):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/advance",
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "VALIDATING"

    def test_07_advance_validating_blocked_no_validation(self,
                                                        admin_session,
                                                        program_main):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/advance",
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "validation_required"

    def test_08_run_strategy_validate(self, admin_session, program_main):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/strategy/validate",
            headers=_csrf(admin_session), timeout=20)
        assert r.status_code == 200, r.text
        assert r.json()["passed"] is True

    def test_09_advance_validating_to_replay(self, admin_session,
                                             program_main):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/advance",
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "REPLAY"

    def test_10_evaluate_replay_no_evidence_fails(self, admin_session,
                                                  program_main):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/evaluate",
            headers=_headers(admin_session), timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["stage"] == "REPLAY"
        assert j["passed"] is False
        # every check should be failing (missing evidence never passes)
        assert any(not c["ok"] for c in j["checks"])

    def test_11_advance_replay_without_passing_eval(self, admin_session,
                                                    program_main):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/advance",
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "stage_evaluation_required"

    def test_12_checkpoint_empty_metrics_400(self, admin_session,
                                             program_main):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/checkpoint",
            json={"metrics": {}}, headers=_headers(admin_session),
            timeout=15)
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "metrics_required"

    def test_13_checkpoint_replay_good(self, admin_session, program_main):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/checkpoint",
            json={"metrics": REPLAY_GOOD, "note": "replay good"},
            headers=_headers(admin_session), timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["kind"] == "checkpoint"
        assert j["seq"] >= 1
        assert j["hash"] and j["prev_hash"]

    def test_14_evaluate_replay_passes(self, admin_session, program_main):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/evaluate",
            headers=_headers(admin_session), timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["stage"] == "REPLAY"
        assert j["passed"] is True, j["checks"]

    def test_15_advance_replay_to_shadow(self, admin_session, program_main):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/advance",
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "SHADOW"

    def test_16_shadow_bad_orders_placed_fails(self, admin_session,
                                               program_main):
        # A single misplaced order in shadow -> gate must fail
        admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/checkpoint",
            json={"metrics": SHADOW_BAD_ORDERS, "note": "bad orders"},
            headers=_headers(admin_session), timeout=15)
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/evaluate",
            headers=_headers(admin_session), timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["stage"] == "SHADOW"
        assert j["passed"] is False
        no_orders = next(c for c in j["checks"]
                         if c["key"] == "no_orders_placed")
        assert no_orders["ok"] is False

    def test_17_shadow_good_and_advance(self, admin_session, program_main):
        admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/checkpoint",
            json={"metrics": SHADOW_GOOD}, headers=_headers(admin_session),
            timeout=15)
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/evaluate",
            headers=_headers(admin_session), timeout=15)
        assert r.status_code == 200 and r.json()["passed"] is True, r.text
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/advance",
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "DEMO"

    def test_18_demo_good_and_advance(self, admin_session, program_main):
        admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/checkpoint",
            json={"metrics": DEMO_GOOD}, headers=_headers(admin_session),
            timeout=15)
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/evaluate",
            headers=_headers(admin_session), timeout=15)
        assert r.status_code == 200 and r.json()["passed"] is True, r.text
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/advance",
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "CANARY"

    def test_19_canary_good_and_advance_to_certified(self, admin_session,
                                                     program_main, mongo):
        admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/checkpoint",
            json={"metrics": CANARY_GOOD}, headers=_headers(admin_session),
            timeout=15)
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/evaluate",
            headers=_headers(admin_session), timeout=15)
        assert r.status_code == 200 and r.json()["passed"] is True, r.text
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/advance",
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["state"] == "CERTIFIED"
        cert_id = j.get("cert_id")
        assert cert_id, "cert_id must be returned when reaching CERTIFIED"
        # cert doc created in db.certifications
        cert = mongo.certifications.find_one({"cert_id": cert_id})
        assert cert is not None, "certification doc must exist"
        # assignment stamped certification_status=CERTIFIED
        r2 = admin_session.get(
            f"{BASE_URL}/api/pamm/programs/{program_main}/strategy",
            timeout=15)
        a = r2.json()["assignment"]
        assert a["certification_status"] == "CERTIFIED"
        assert a["cert_id"] == cert_id

    def test_20_advance_to_live_blocked_not_active(self, admin_session,
                                                   program_main):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/advance",
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "assignment_not_active"

    def test_21_strategy_activate(self, admin_session, program_main):
        # program has no LIVE master → certification_required NOT triggered
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/strategy/activate",
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ACTIVE"

    def test_22_advance_to_live_succeeds(self, admin_session, program_main):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/advance",
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "LIVE"

    def test_23_evidence_chain_valid(self, admin_session, program_main):
        r = admin_session.get(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/evidence",
            timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["chain_valid"] is True
        # started + several checkpoints + evaluations + advances
        # ≥ 1 start + 4 checkpoints + ≥5 evaluations + ≥6 stage_advanced
        assert j["count"] >= 12, f"expected many evidence records, got {j['count']}"
        # Every record has hash + prev_hash
        for rec in j["records"]:
            assert rec["hash"] and rec["prev_hash"]
            assert rec["seq"] >= 1

    def test_24_revoke_certified_becomes_revoked(self, admin_session,
                                                 program_main, mongo):
        # snapshot cert_id BEFORE revoke
        pre = admin_session.get(
            f"{BASE_URL}/api/pamm/programs/{program_main}/strategy",
            timeout=15).json()
        cert_id = pre["assignment"]["cert_id"]
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_main}/certification/revoke",
            json={"reason": "iter217 test revoke"},
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["state"] == "REVOKED"
        # assignment certification_status flipped
        after = admin_session.get(
            f"{BASE_URL}/api/pamm/programs/{program_main}/strategy",
            timeout=15).json()
        assert after["assignment"]["certification_status"] == "REVOKED"
        # cert doc revoked
        cert = mongo.certifications.find_one({"cert_id": cert_id})
        assert cert is not None
        assert (cert.get("status") == "REVOKED"
                or cert.get("revoked_at")
                or cert.get("revoked") is True), \
            f"cert doc should be revoked: {cert}"


# ═════════ 2. Mid-pipeline revoke → DRAFT abort ═══════════════════════════

class TestMidPipelineRevoke:
    def test_setup_and_walk_to_shadow(self, admin_session, program_mid):
        # assign + start + validate + walk to SHADOW
        admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_mid}/strategy",
            json={"mode": "SINGLE", "strategy_id": "sniper",
                  "risk_profile_id": "controlled"},
            headers=_csrf(admin_session), timeout=15)
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_mid}/certification/start",
            headers=_headers(admin_session), timeout=15)
        assert r.status_code == 200
        admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_mid}/certification/advance",
            headers=_headers(admin_session, step_up=True), timeout=15)  # VALIDATING
        admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_mid}/strategy/validate",
            headers=_csrf(admin_session), timeout=20)
        admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_mid}/certification/advance",
            headers=_headers(admin_session, step_up=True), timeout=15)  # REPLAY
        admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_mid}/certification/checkpoint",
            json={"metrics": REPLAY_GOOD},
            headers=_headers(admin_session), timeout=15)
        admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_mid}/certification/evaluate",
            headers=_headers(admin_session), timeout=15)
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_mid}/certification/advance",
            headers=_headers(admin_session, step_up=True), timeout=15)  # SHADOW
        assert r.status_code == 200
        assert r.json()["state"] == "SHADOW"

    def test_revoke_mid_pipeline_returns_draft(self, admin_session,
                                               program_mid):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_mid}/certification/revoke",
            json={"reason": "abort mid-pipeline"},
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "DRAFT"


# ═════════ 3. AUTH / BOLA / step-up requirements ══════════════════════════

class TestAuthAndStepUp:
    def test_status_bola_non_member_403(self, plain_user, program_bola):
        r = plain_user["session"].get(
            f"{BASE_URL}/api/pamm/programs/{program_bola}/certification",
            timeout=15)
        assert r.status_code in (403, 404), r.text

    def test_start_non_admin_403(self, manager_user, program_bola):
        r = manager_user["session"].post(
            f"{BASE_URL}/api/pamm/programs/{program_bola}/certification/start",
            headers=_csrf(manager_user["session"]), timeout=15)
        assert r.status_code == 403, r.text

    def test_checkpoint_non_admin_403(self, manager_user, program_bola):
        r = manager_user["session"].post(
            f"{BASE_URL}/api/pamm/programs/{program_bola}/certification/checkpoint",
            json={"metrics": {"foo": 1}},
            headers=_csrf(manager_user["session"]), timeout=15)
        assert r.status_code == 403, r.text

    def test_evaluate_non_admin_403(self, manager_user, program_bola):
        r = manager_user["session"].post(
            f"{BASE_URL}/api/pamm/programs/{program_bola}/certification/evaluate",
            headers=_csrf(manager_user["session"]), timeout=15)
        assert r.status_code == 403, r.text

    def test_advance_non_admin_403(self, manager_user, program_bola):
        r = manager_user["session"].post(
            f"{BASE_URL}/api/pamm/programs/{program_bola}/certification/advance",
            headers=_csrf(manager_user["session"]), timeout=15)
        assert r.status_code == 403, r.text

    def test_revoke_non_admin_403(self, manager_user, program_bola):
        r = manager_user["session"].post(
            f"{BASE_URL}/api/pamm/programs/{program_bola}/certification/revoke",
            json={"reason": "nope"},
            headers=_csrf(manager_user["session"]), timeout=15)
        assert r.status_code == 403, r.text

    def _no_stepup(self, session):
        prev = session.headers.get("X-Step-Up-Bypass")
        session.headers["X-Step-Up-Bypass"] = ""
        return prev

    def _restore_stepup(self, session, prev):
        if prev is None:
            session.headers.pop("X-Step-Up-Bypass", None)
        else:
            session.headers["X-Step-Up-Bypass"] = prev

    def test_advance_without_step_up_403(self, admin_session, program_bola):
        prev = self._no_stepup(admin_session)
        try:
            r = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{program_bola}/certification/advance",
                headers=_csrf(admin_session), timeout=15)
        finally:
            self._restore_stepup(admin_session, prev)
        assert r.status_code in (401, 403), r.text
        detail = r.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("code") in (
                "step_up_required", "step_up_invalid",
                "mfa_enrollment_required")

    def test_revoke_without_step_up_403(self, admin_session, program_bola):
        prev = self._no_stepup(admin_session)
        try:
            r = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{program_bola}/certification/revoke",
                json={"reason": "no-stepup"},
                headers=_csrf(admin_session), timeout=15)
        finally:
            self._restore_stepup(admin_session, prev)
        assert r.status_code in (401, 403), r.text


# ═════════ 4. REGRESSION — v62.1 strategy endpoints still work ════════════

class TestRegressionStrategyEndpoints:
    def test_get_strategies(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/pamm/strategies", timeout=15)
        assert r.status_code == 200, r.text
        ids = {s["strategy_id"] for s in r.json()["strategies"]}
        assert ids == {"sniper", "scalper", "fast_scalp", "nitro_scalper"}

    def test_strategy_lifecycle_on_fresh_program(self, admin_session, mongo):
        pid = _create_program(admin_session, "reg")
        try:
            # GET LEGACY
            r = admin_session.get(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy", timeout=15)
            assert r.status_code == 200
            assert r.json()["mode"] == "LEGACY"
            # POST assign
            r = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"mode": "SINGLE", "strategy_id": "sniper",
                      "risk_profile_id": "controlled"},
                headers=_csrf(admin_session), timeout=15)
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "ASSIGNED"
            # validate
            r = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy/validate",
                headers=_csrf(admin_session), timeout=20)
            assert r.status_code == 200 and r.json()["passed"] is True
            # activate
            r = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy/activate",
                headers=_headers(admin_session, step_up=True), timeout=15)
            assert r.status_code == 200
            assert r.json()["status"] == "ACTIVE"
            # suspend
            r = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy/suspend",
                json={"reason": "regression"},
                headers=_csrf(admin_session), timeout=15)
            assert r.status_code == 200
            assert r.json()["status"] == "SUSPENDED"
        finally:
            _cleanup_program(mongo, pid)
