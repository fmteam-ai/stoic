"""iter-216 / v62.1 — HTTP end-to-end tests for PAMM Strategy Profiles.

Covers:
  * GET /api/pamm/strategies — 4 strategies, magic codes 62001-62004,
    strategy_hash, distinct characteristics + gates, feature_flags,
    risk_profiles (conservative/controlled/growth). Non-manager -> 403.
  * GET /api/pamm/strategies/nitro-eligibility — score/components(8)/
    status/thresholds. BOLA: another user's account_id -> 404.
  * GET/POST/PATCH /api/pamm/programs/{id}/strategy — LEGACY get,
    SINGLE assign with pinned version+hash, duplicate 409, bad inputs
    400, PATCH updates + version bump, unknown profile 400.
  * POST /strategy/validate — 4 checks; execution_eligibility added for
    nitro_scalper.
  * POST /strategy/activate — step-up MFA required (bypass token used);
    nitro_scalper activation rejected 403 nitro_live_disabled.
  * POST /strategy/suspend — 409 no_active_assignment when not ACTIVE.
  * POST /strategy/change — 409 program_not_flat with a seeded OPEN
    trade on master_account_id; when flat, old REPLACED + new created.
  * Audit: /api/pamm/events shows PAMM_STRATEGY_ASSIGNED + VALIDATED.
  * BOLA: non-manager other user -> 403/404 on all /strategy* endpoints.
"""
import os
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
    email = f"test_iter216_{uuid.uuid4().hex[:10]}@example.com"
    password = f"Iter216-{uuid.uuid4().hex[:12]}-Zx"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": password,
                            "name": "Iter216 Tester",
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
    """A non-manager, non-admin user (should be blocked from most PAMM)."""
    u = _make_user(mongo, pamm_manager=False)
    yield u
    mongo.users.delete_one({"_id": ObjectId(u["id"])})
    mongo.accounts.delete_many({"user_id": u["id"]})


@pytest.fixture(scope="module")
def manager_user(mongo):
    """Non-admin PAMM manager (owns no other user's programs)."""
    u = _make_user(mongo, pamm_manager=True)
    yield u
    mongo.users.delete_one({"_id": ObjectId(u["id"])})
    mongo.accounts.delete_many({"user_id": u["id"]})


def _create_program(admin_session, mongo, name_suffix):
    """Create a fresh PAMM program via API. Yields program_id, cleans up."""
    name = f"TEST_iter216_{name_suffix}_{uuid.uuid4().hex[:6]}"
    r = admin_session.post(f"{BASE_URL}/api/pamm/programs",
                           json={"name": name},
                           headers=_csrf(admin_session), timeout=20)
    assert r.status_code in (200, 201), f"create program: {r.status_code} {r.text}"
    pgm = r.json()
    # tests must not depend on the live news calendar — disable blackout
    mongo.pamm_programs.update_one(
        {"program_id": pgm["program_id"]},
        {"$set": {"risk_limits.news_filter.enabled": False}})
    return pgm["program_id"], name


@pytest.fixture(scope="module")
def program_a(admin_session, mongo):
    """A program used for happy-path assign/validate/activate/patch tests."""
    pid, name = _create_program(admin_session, mongo, "A")
    yield pid
    # Cleanup
    mongo.pamm_programs.delete_one({"program_id": pid})
    mongo.pamm_master_accounts.delete_many({"program_id": pid})
    mongo.pamm_strategy_assignments.delete_many({"pamm_program_id": pid})
    mongo.pamm_events.delete_many({"payload.program_id": pid})


@pytest.fixture(scope="module")
def program_nitro(admin_session, mongo):
    """A program used to prove nitro activation is rejected."""
    pid, name = _create_program(admin_session, mongo, "N")
    yield pid
    mongo.pamm_programs.delete_one({"program_id": pid})
    mongo.pamm_master_accounts.delete_many({"program_id": pid})
    mongo.pamm_strategy_assignments.delete_many({"pamm_program_id": pid})
    mongo.pamm_events.delete_many({"payload.program_id": pid})


@pytest.fixture(scope="module")
def program_change(admin_session, mongo):
    """A program used to test /strategy/change flat-guard."""
    pid, name = _create_program(admin_session, mongo, "C")
    # Attach a master_account_id so the flat-guard has something to check
    master_acc_id = f"iter216_master_{uuid.uuid4().hex[:8]}"
    mongo.pamm_programs.update_one(
        {"program_id": pid},
        {"$set": {"master_account_id": master_acc_id}})
    yield pid, master_acc_id
    mongo.pamm_programs.delete_one({"program_id": pid})
    mongo.pamm_master_accounts.delete_many({"program_id": pid})
    mongo.pamm_strategy_assignments.delete_many({"pamm_program_id": pid})
    mongo.trades.delete_many({"account_id": master_acc_id})
    mongo.pamm_events.delete_many({"payload.program_id": pid})


# ═══════════════ 1. /api/pamm/strategies ══════════════════════════════

class TestStrategiesEndpoint:
    def test_admin_lists_4_strategies_with_hash_and_magic(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/pamm/strategies", timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        strategies = j["strategies"]
        ids = {s["strategy_id"] for s in strategies}
        assert ids == {"sniper", "scalper", "fast_scalp", "nitro_scalper"}
        for s in strategies:
            assert s["version"] == "1.0.0"
            assert isinstance(s["strategy_hash"], str) and len(s["strategy_hash"]) == 32
            assert 62000 < s["magic_code"] < 62100
        codes = [s["magic_code"] for s in strategies]
        assert len(set(codes)) == 4

    def test_characteristics_and_gates_are_distinct(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/pamm/strategies", timeout=15)
        j = r.json()
        import json
        chars = {json.dumps(s.get("characteristics", {}), sort_keys=True)
                 for s in j["strategies"]}
        gates = {tuple(s.get("gates", [])) for s in j["strategies"]}
        assert len(chars) == 4, "characteristics must be genuinely different"
        assert len(gates) == 4, "gates must be different too"

    def test_feature_flags_match_spec(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/pamm/strategies", timeout=15)
        ff = r.json()["feature_flags"]
        assert ff == {"PAMM_STRATEGY_ASSIGNMENT": True,
                      "PAMM_MULTI_STRATEGY": False,
                      "PAMM_DYNAMIC_AI": False,
                      "PAMM_NITRO_LIVE": False,
                      "PAMM_REQUIRE_CERTIFICATION": True}

    def test_risk_profiles_present(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/pamm/strategies", timeout=15)
        profiles = {p["risk_profile_id"] for p in r.json()["risk_profiles"]}
        assert profiles == {"conservative", "controlled", "growth"}

    def test_non_manager_403(self, plain_user):
        r = plain_user["session"].get(f"{BASE_URL}/api/pamm/strategies",
                                       timeout=15)
        assert r.status_code == 403


# ═══════════════ 2. /api/pamm/strategies/nitro-eligibility ══════════════

class TestNitroEligibility:
    def test_shape_and_status(self, admin_session):
        r = admin_session.get(
            f"{BASE_URL}/api/pamm/strategies/nitro-eligibility", timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert isinstance(j["score"], (int, float))
        assert set(j["components"].keys()) >= {
            "latency_quality", "infrastructure_health", "spread_quality",
            "broker_quality", "slippage_quality", "liquidity",
            "market_quality", "regime_compatibility"}
        assert len(j["components"]) == 8
        assert j["status"] in {"NITRO_ENABLED", "NITRO_REDUCED",
                                "NITRO_PAUSED"}
        assert set(j["thresholds"].keys()) == {
            "enabled_min", "reduced_min", "hard_floor"}

    def test_bola_other_users_account_404(self, manager_user, mongo,
                                          plain_user):
        # Insert an account owned by plain_user; manager_user tries to read it
        acc_id = ObjectId()
        mongo.accounts.insert_one({
            "_id": acc_id, "user_id": plain_user["id"],
            "broker": "TestBroker216",
            "bridge_token": f"bt_216_{uuid.uuid4().hex}",
            "mode": "paper"})
        try:
            r = manager_user["session"].get(
                f"{BASE_URL}/api/pamm/strategies/nitro-eligibility",
                params={"account_id": str(acc_id)}, timeout=15)
            assert r.status_code == 404, f"expected BOLA 404, got {r.status_code}: {r.text}"
        finally:
            mongo.accounts.delete_one({"_id": acc_id})


# ═══════════════ 3. Program strategy assignment lifecycle ═══════════════

class TestAssignmentLifecycle:
    def test_get_legacy_when_no_assignment(self, admin_session, program_a):
        r = admin_session.get(
            f"{BASE_URL}/api/pamm/programs/{program_a}/strategy", timeout=15)
        assert r.status_code == 200
        j = r.json()
        assert j["mode"] == "LEGACY"
        assert j["assignment"] is None

    def test_assign_sniper_pins_version_and_hash(self, admin_session,
                                                 program_a):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_a}/strategy",
            json={"mode": "SINGLE", "strategy_id": "sniper",
                  "risk_profile_id": "controlled"},
            headers=_csrf(admin_session), timeout=15)
        assert r.status_code == 200, r.text
        a = r.json()
        assert a["strategy_id"] == "sniper"
        assert a["strategy_version"] == "1.0.0"
        assert isinstance(a["strategy_hash"], str) and len(a["strategy_hash"]) == 32
        assert a["status"] == "ASSIGNED"
        assert a["certification_status"] == "UNCERTIFIED"
        assert a["risk_profile_id"] == "controlled"
        assert a["mode"] == "SINGLE"

    def test_duplicate_assignment_409(self, admin_session, program_a):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_a}/strategy",
            json={"mode": "SINGLE", "strategy_id": "scalper",
                  "risk_profile_id": "controlled"},
            headers=_csrf(admin_session), timeout=15)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "assignment_exists"

    def test_bogus_strategy_400(self, admin_session, mongo):
        pid, _ = _create_program(admin_session, mongo, "bogus_strat")
        try:
            r = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"mode": "SINGLE", "strategy_id": "bogus",
                      "risk_profile_id": "controlled"},
                headers=_csrf(admin_session), timeout=15)
            assert r.status_code == 400
            assert r.json()["detail"]["error"] == "strategy_not_eligible"
        finally:
            mongo.pamm_programs.delete_one({"program_id": pid})
            mongo.pamm_master_accounts.delete_many({"program_id": pid})
            mongo.pamm_events.delete_many({"payload.program_id": pid})

    def test_bogus_risk_profile_400(self, admin_session, mongo):
        pid, _ = _create_program(admin_session, mongo, "bogus_rp")
        try:
            r = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"mode": "SINGLE", "strategy_id": "sniper",
                      "risk_profile_id": "bogus"},
                headers=_csrf(admin_session), timeout=15)
            assert r.status_code == 400
            assert r.json()["detail"]["error"] == "unknown_risk_profile"
        finally:
            mongo.pamm_programs.delete_one({"program_id": pid})
            mongo.pamm_master_accounts.delete_many({"program_id": pid})
            mongo.pamm_events.delete_many({"payload.program_id": pid})

    def test_multi_mode_gated_400(self, admin_session, mongo):
        pid, _ = _create_program(admin_session, mongo, "multi_mode")
        try:
            r = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"mode": "MULTI", "strategy_id": "sniper",
                      "risk_profile_id": "controlled"},
                headers=_csrf(admin_session), timeout=15)
            assert r.status_code == 400
            assert r.json()["detail"]["error"] == "mode_not_enabled"
        finally:
            mongo.pamm_programs.delete_one({"program_id": pid})
            mongo.pamm_master_accounts.delete_many({"program_id": pid})
            mongo.pamm_events.delete_many({"payload.program_id": pid})

    def test_validate_sniper_passes(self, admin_session, program_a):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_a}/strategy/validate",
            headers=_csrf(admin_session), timeout=20)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["passed"] is True
        keys = {c["key"] for c in j["checks"]}
        assert {"strategy_registered", "pamm_eligible", "version_pin",
                "risk_profile"} <= keys
        # sniper does NOT require latency cert -> no execution_eligibility
        assert "execution_eligibility" not in keys

    def test_patch_weights_rejected_in_single(self, admin_session,
                                              program_a):
        # v62.3 — SINGLE mode pins weights (min=0, target=1, max=1)
        r = admin_session.patch(
            f"{BASE_URL}/api/pamm/programs/{program_a}/strategy",
            json={"risk_profile_id": "growth", "target_weight": 0.8},
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "weights_fixed_in_single"

    def test_patch_material_change_requires_revalidation(self,
                                                         admin_session,
                                                         program_a):
        # v62.3 — a risk-profile change invalidates prior validation/
        # certification: REVALIDATION_REQUIRED, last_validation cleared
        r = admin_session.patch(
            f"{BASE_URL}/api/pamm/programs/{program_a}/strategy",
            json={"risk_profile_id": "growth"},
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["risk_profile_id"] == "growth"
        assert int(j["version"]) >= 2
        assert j["revalidation_required"] is True
        assert j["last_validation"] is None
        # re-validate so downstream activation tests keep working
        rv = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_a}/strategy/validate",
            headers=_csrf(admin_session), timeout=20)
        assert rv.status_code == 200 and rv.json()["passed"] is True

    def test_patch_bogus_profile_400(self, admin_session, program_a):
        r = admin_session.patch(
            f"{BASE_URL}/api/pamm/programs/{program_a}/strategy",
            json={"risk_profile_id": "bogus"},
            headers=_csrf(admin_session), timeout=15)
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "unknown_risk_profile"

    def test_activate_requires_step_up_without_bypass(self, admin_session,
                                                     program_a):
        # Disable conftest's auto-injected bypass to prove the REAL gate
        # rejects when no fresh MFA is present.
        prev = admin_session.headers.get("X-Step-Up-Bypass")
        admin_session.headers["X-Step-Up-Bypass"] = ""
        try:
            r = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{program_a}/strategy/activate",
                headers=_csrf(admin_session), timeout=15)
        finally:
            if prev is None:
                admin_session.headers.pop("X-Step-Up-Bypass", None)
            else:
                admin_session.headers["X-Step-Up-Bypass"] = prev
        assert r.status_code in (401, 403), r.text
        detail = r.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("code") in ("mfa_enrollment_required",
                                          "step_up_required",
                                          "step_up_invalid")

    def test_activate_with_bypass_succeeds(self, admin_session, program_a):
        assert STEP_UP_BYPASS, "STEP_UP_BYPASS_TOKEN needed for this test"
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_a}/strategy/activate",
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ACTIVE"

    def test_suspend_non_active_returns_409(self, admin_session, mongo):
        # Fresh program, assign but do NOT activate → suspend → 409
        pid, _ = _create_program(admin_session, mongo, "susp")
        try:
            admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"mode": "SINGLE", "strategy_id": "sniper",
                      "risk_profile_id": "controlled"},
                headers=_csrf(admin_session), timeout=15)
            r = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy/suspend",
                json={"reason": "test"},
                headers=_csrf(admin_session), timeout=15)
            assert r.status_code == 409
            assert r.json()["detail"]["error"] == "no_active_assignment"
        finally:
            mongo.pamm_programs.delete_one({"program_id": pid})
            mongo.pamm_master_accounts.delete_many({"program_id": pid})
            mongo.pamm_strategy_assignments.delete_many({"pamm_program_id": pid})
            mongo.pamm_events.delete_many({"payload.program_id": pid})


# ═══════════════ 4. Nitro strategy — validation + activation gating ═════

class TestNitroStrategyGating:
    def test_assign_nitro_scalper(self, admin_session, program_nitro):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_nitro}/strategy",
            json={"mode": "SINGLE", "strategy_id": "nitro_scalper",
                  "risk_profile_id": "controlled"},
            headers=_csrf(admin_session), timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["strategy_id"] == "nitro_scalper"

    def test_validate_includes_execution_eligibility(self, admin_session,
                                                    program_nitro):
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_nitro}/strategy/validate",
            headers=_csrf(admin_session), timeout=20)
        assert r.status_code == 200, r.text
        j = r.json()
        keys = {c["key"] for c in j["checks"]}
        assert "execution_eligibility" in keys
        exec_chk = next(c for c in j["checks"] if c["key"] == "execution_eligibility")
        assert "score" in exec_chk["detail"]
        assert "status" in exec_chk["detail"]

    def test_activate_nitro_rejected_by_feature_flag(self, admin_session,
                                                    program_nitro):
        assert STEP_UP_BYPASS
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{program_nitro}/strategy/activate",
            headers=_headers(admin_session, step_up=True), timeout=15)
        # nitro activation blocked by PAMM_NITRO_LIVE=false → 403
        # (Only if the assignment is validated & passed. If validation
        # didn't pass, code returns 409 validation_required.)
        assert r.status_code in (403, 409), r.text
        if r.status_code == 403:
            assert r.json()["detail"]["error"] == "nitro_live_disabled"


# ═══════════════ 5. Change (flat guard) ═════════════════════════════════

class TestStrategyChangeFlatGuard:
    def test_change_with_open_trade_returns_409(self, admin_session,
                                                program_change, mongo):
        pid, master_acc_id = program_change
        # Seed an initial assignment
        admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
            json={"mode": "SINGLE", "strategy_id": "sniper",
                  "risk_profile_id": "controlled"},
            headers=_csrf(admin_session), timeout=15)
        # Seed an OPEN trade on the master account
        mongo.trades.insert_one({
            "trade_id": f"iter216_open_{uuid.uuid4().hex[:8]}",
            "account_id": master_acc_id,
            "status": "open",
            "symbol": "EURUSD",
            "created_at": datetime.now(timezone.utc).isoformat()})
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{pid}/strategy/change",
            json={"strategy_id": "scalper"},
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "program_not_flat"
        assert r.json()["detail"]["open_positions"] >= 1

    def test_change_when_flat_replaces_old(self, admin_session,
                                           program_change, mongo):
        pid, master_acc_id = program_change
        # Delete the open trade → program becomes flat
        mongo.trades.delete_many({"account_id": master_acc_id})
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{pid}/strategy/change",
            json={"strategy_id": "scalper", "risk_profile_id": "controlled"},
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 200, r.text
        new = r.json()
        assert new["strategy_id"] == "scalper"
        # Old assignment should be REPLACED
        old = mongo.pamm_strategy_assignments.find_one(
            {"pamm_program_id": pid, "strategy_id": "sniper"})
        assert old and old["status"] == "REPLACED"


# ═══════════════ 6. Audit trail ═════════════════════════════════════════

class TestAuditTrail:
    def test_events_after_assign_and_validate(self, admin_session, program_a):
        r = admin_session.get(f"{BASE_URL}/api/pamm/events",
                              params={"program_id": program_a}, timeout=15)
        assert r.status_code == 200
        types = {e["type"] for e in r.json()["events"]}
        assert "PAMM_STRATEGY_ASSIGNED" in types
        assert "PAMM_STRATEGY_VALIDATED" in types


# ═══════════════ 7. BOLA — non-manager on strategy endpoints ═════════════

class TestBolaNonManager:
    def test_plain_user_get_strategy_403(self, plain_user, program_a):
        r = plain_user["session"].get(
            f"{BASE_URL}/api/pamm/programs/{program_a}/strategy", timeout=15)
        # get_program_strategy_ep uses require_program_access; plain user
        # is neither admin nor the manager → 403
        assert r.status_code in (403, 404), r.text

    def test_plain_user_post_strategy_403(self, plain_user, program_a):
        r = plain_user["session"].post(
            f"{BASE_URL}/api/pamm/programs/{program_a}/strategy",
            json={"mode": "SINGLE", "strategy_id": "sniper",
                  "risk_profile_id": "controlled"},
            headers=_csrf(plain_user["session"]), timeout=15)
        assert r.status_code in (403, 404), r.text

    def test_plain_user_activate_403(self, plain_user, program_a):
        r = plain_user["session"].post(
            f"{BASE_URL}/api/pamm/programs/{program_a}/strategy/activate",
            headers=_csrf(plain_user["session"]), timeout=15)
        assert r.status_code in (403, 404), r.text

    def test_unknown_program_id_404(self, admin_session):
        r = admin_session.get(
            f"{BASE_URL}/api/pamm/programs/pgm_does_not_exist/strategy",
            timeout=15)
        assert r.status_code == 404
