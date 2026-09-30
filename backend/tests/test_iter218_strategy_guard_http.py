from live_target import ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-218 / v62.3 — HTTP end-to-end tests for the PAMM Strategy
Execution Guard and its material-change governance.

Covers ONLY the NEW HTTP/E2E behaviours added in v62.3:
  * PATCH governance: step-up required; risk-profile change bumps
    version, clears last_validation, sets REVALIDATION_REQUIRED, and
    revokes a CERTIFIED cert doc when present.
  * Weight pinning: POST target_weight -> 400 weights_fixed_in_single;
    PATCH weights -> 400; assignments store SINGLE_WEIGHTS on disk.
  * Validation account-scoped + policy-based per strategy: fast_scalp
    HIGH, scalper MODERATE, sniper NO execution_eligibility.
  * LIVE activation enforcement: certification_required 409 on a LIVE
    account; flip to DEMO -> activation succeeds.
  * Execution guard E2E via POST /api/trades/manual on a paper account
    that IS a PAMM master: LEGACY passthrough, STRATEGY not-ACTIVE
    rejection, STRATEGY ACTIVE stamping, max_open_positions cap.
  * Strategy-ownership endpoint: counts, healthy flag, BOLA scoping.
  * Regression: non-PAMM manual trade is unaffected; feature_flags
    surface PAMM_REQUIRE_CERTIFICATION:true.
"""
import os
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
STEP_UP_BYPASS = os.environ.get("STEP_UP_BYPASS_TOKEN", "")

ADMIN_EMAIL = "admin@stoicaibot.com"
pass  # ADMIN_PASSWORD comes from live_target
# ─────────── fixtures ────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def mongo():
    c = MongoClient(MONGO_URL)
    yield c[DB_NAME]
    c.close()


def _login(session, email, password):
    r = session.post(f"{BASE_URL}/api/auth/login",
                     json={"email": email, "password": password}, timeout=15)
    assert r.status_code == 200, f"login {r.status_code}: {r.text}"
    return r.json()


def _csrf(session):
    tok = session.cookies.get("csrf_token")
    assert tok, "csrf_token cookie missing after login"
    return {"X-CSRF-Token": tok}


def _headers(session, step_up=False):
    h = _csrf(session)
    if step_up and STEP_UP_BYPASS:
        h["X-Step-Up-Bypass"] = STEP_UP_BYPASS
    return h


def _no_stepup(session):
    prev = session.headers.get("X-Step-Up-Bypass")
    session.headers["X-Step-Up-Bypass"] = ""
    return prev


def _restore_stepup(session, prev):
    if prev is None:
        session.headers.pop("X-Step-Up-Bypass", None)
    else:
        session.headers["X-Step-Up-Bypass"] = prev


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    _login(s, ADMIN_EMAIL, ADMIN_PASSWORD)
    yield s


@pytest.fixture(scope="module")
def admin_id(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/auth/me", timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _make_user(mongo):
    email = f"test_iter218_{uuid.uuid4().hex[:10]}@example.com"
    password = f"Iter218-{uuid.uuid4().hex[:12]}-Zx"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": password,
                            "name": "Iter218 Tester",
                            "terms_agreed": True}, timeout=15)
    assert r.status_code in (200, 201), r.text
    mongo.users.update_one({"email": email},
                           {"$set": {"email_verified": True}})
    doc = mongo.users.find_one({"email": email})
    s = requests.Session()
    _login(s, email, password)
    return {"session": s, "email": email, "id": str(doc["_id"])}


@pytest.fixture(scope="module")
def plain_user(mongo):
    u = _make_user(mongo)
    yield u
    mongo.users.delete_one({"_id": ObjectId(u["id"])})


def _create_program(admin_session, suffix):
    name = f"TEST_iter218_{suffix}_{uuid.uuid4().hex[:6]}"
    r = admin_session.post(f"{BASE_URL}/api/pamm/programs",
                           json={"name": name},
                           headers=_csrf(admin_session), timeout=20)
    assert r.status_code in (200, 201), r.text
    pid = r.json()["program_id"]
    # tests must not depend on the live news calendar — disable blackout
    c = MongoClient(MONGO_URL)
    c[DB_NAME].pamm_programs.update_one(
        {"program_id": pid},
        {"$set": {"risk_limits.news_filter.enabled": False}})
    c.close()
    return pid


def _insert_paper_account(mongo, admin_id, broker_env="DEMO"):
    """Create a minimal paper account owned by admin. `broker_environment`
    is set explicitly so broker_env classification is deterministic."""
    acc_id = ObjectId()
    mongo.accounts.insert_one({
        "_id": acc_id, "user_id": admin_id,
        "broker": "TestBroker218",
        "server": "TestServer218-Demo",
        "mode": "paper",
        "trading_enabled": True,   # AT-01: enablement must be explicitly true
        "broker_environment": broker_env,
        "bridge_token": f"bt_218_{uuid.uuid4().hex}",
        "account_number": f"iter218_{uuid.uuid4().hex[:6]}",
    })
    return str(acc_id)


def _bind_master(mongo, program_id, account_id):
    mongo.pamm_programs.update_one(
        {"program_id": program_id},
        {"$set": {"master_account_id": account_id}})


def _cleanup_program(mongo, pid):
    mongo.pamm_programs.delete_one({"program_id": pid})
    mongo.pamm_master_accounts.delete_many({"program_id": pid})
    for a in list(mongo.pamm_strategy_assignments.find(
            {"pamm_program_id": pid}, {"cert_id": 1})):
        if a.get("cert_id"):
            mongo.certifications.delete_many({"cert_id": a["cert_id"]})
    mongo.pamm_strategy_assignments.delete_many({"pamm_program_id": pid})
    mongo.strategy_cert_campaigns.delete_many({"pamm_program_id": pid})
    mongo.strategy_cert_evidence.delete_many(
        {"pamm_program_id": pid})
    mongo.pamm_events.delete_many({"payload.program_id": pid})


def _cleanup_account(mongo, acc_id):
    mongo.trades.delete_many({"account_id": acc_id})
    mongo.execution_intents.delete_many({"account_id": acc_id})
    mongo.authority_snapshots.delete_many({})  # global small; noop is fine
    try:
        mongo.accounts.delete_one({"_id": ObjectId(acc_id)})
    except Exception:
        mongo.accounts.delete_one({"_id": acc_id})


# ═════ 1. PATCH governance ══════════════════════════════════════════════

class TestPatchGovernance:
    """PATCH /strategy — step-up gate + material-change revalidation
    (+ cert revoke when CERTIFIED)."""

    @pytest.fixture(scope="class")
    def prog(self, admin_session, mongo):
        pid = _create_program(admin_session, "patch")
        # assign sniper + controlled, validate to establish
        # last_validation.passed=true
        admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
            json={"mode": "SINGLE", "strategy_id": "sniper",
                  "risk_profile_id": "controlled"},
            headers=_csrf(admin_session), timeout=15)
        admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{pid}/strategy/validate",
            headers=_csrf(admin_session), timeout=20)
        yield pid
        _cleanup_program(mongo, pid)

    def test_patch_without_stepup_403(self, admin_session, prog):
        prev = _no_stepup(admin_session)
        try:
            r = admin_session.patch(
                f"{BASE_URL}/api/pamm/programs/{prog}/strategy",
                json={"risk_profile_id": "growth"},
                headers=_csrf(admin_session), timeout=15)
        finally:
            _restore_stepup(admin_session, prev)
        assert r.status_code in (401, 403), r.text
        detail = r.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("code") in (
                "step_up_required", "step_up_invalid",
                "mfa_enrollment_required")

    def test_patch_with_stepup_revalidation_and_version_bump(
            self, admin_session, prog, mongo):
        pre = mongo.pamm_strategy_assignments.find_one(
            {"pamm_program_id": prog})
        pre_ver = int(pre.get("version") or 1)
        r = admin_session.patch(
            f"{BASE_URL}/api/pamm/programs/{prog}/strategy",
            json={"risk_profile_id": "growth"},
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["risk_profile_id"] == "growth"
        assert j["revalidation_required"] is True
        assert j["last_validation"] is None
        assert int(j["version"]) == pre_ver + 1

    def test_patch_certified_revokes_cert(self, admin_session, mongo):
        """Simulate a CERTIFIED assignment (fake cert doc + stamp), then
        PATCH risk_profile → certification_status becomes
        REVALIDATION_REQUIRED and the cert doc is revoked."""
        pid = _create_program(admin_session, "patch_cert")
        try:
            # assign + validate
            admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"mode": "SINGLE", "strategy_id": "sniper",
                      "risk_profile_id": "controlled"},
                headers=_csrf(admin_session), timeout=15)
            admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy/validate",
                headers=_csrf(admin_session), timeout=20)
            # inject a fake CERTIFIED cert + stamp the assignment
            cert_id = f"cert_iter218_{uuid.uuid4().hex[:10]}"
            from datetime import datetime, timedelta, timezone
            future = (datetime.now(timezone.utc)
                      + timedelta(days=90)).isoformat()
            mongo.certifications.insert_one({
                "cert_id": cert_id, "kind": "pamm_strategy",
                "subject": pid, "issued_at":
                    datetime.now(timezone.utc).isoformat(),
                "expires_at": future, "revoked": False,
                "status": "ACTIVE"})
            mongo.pamm_strategy_assignments.update_one(
                {"pamm_program_id": pid},
                {"$set": {"certification_status": "CERTIFIED",
                          "cert_id": cert_id}})
            # PATCH risk profile → material change
            r = admin_session.patch(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"risk_profile_id": "growth"},
                headers=_headers(admin_session, step_up=True), timeout=15)
            assert r.status_code == 200, r.text
            j = r.json()
            assert j["revalidation_required"] is True
            assert j["last_validation"] is None
            # certification_status flipped
            a = mongo.pamm_strategy_assignments.find_one(
                {"pamm_program_id": pid})
            assert a["certification_status"] == "REVALIDATION_REQUIRED"
            # cert revoked
            cert = mongo.certifications.find_one({"cert_id": cert_id})
            assert cert is not None
            assert (cert.get("status") == "REVOKED"
                    or cert.get("revoked") is True
                    or cert.get("revoked_at")), \
                f"cert should be revoked: {cert}"
        finally:
            _cleanup_program(mongo, pid)


# ═════ 2. Weight pinning ════════════════════════════════════════════════

class TestWeightPinning:
    @pytest.fixture(scope="class")
    def prog(self, admin_session, mongo):
        pid = _create_program(admin_session, "weights")
        yield pid
        _cleanup_program(mongo, pid)

    def test_post_with_target_weight_400(self, admin_session, mongo):
        pid = _create_program(admin_session, "wpost")
        try:
            r = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"mode": "SINGLE", "strategy_id": "sniper",
                      "risk_profile_id": "controlled",
                      "target_weight": 0.8},
                headers=_csrf(admin_session), timeout=15)
            assert r.status_code == 400, r.text
            assert r.json()["detail"]["error"] == "weights_fixed_in_single"
        finally:
            _cleanup_program(mongo, pid)

    def test_stored_weights_are_pinned(self, admin_session, prog, mongo):
        # assign clean (no weight field)
        r = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{prog}/strategy",
            json={"mode": "SINGLE", "strategy_id": "sniper",
                  "risk_profile_id": "controlled"},
            headers=_csrf(admin_session), timeout=15)
        assert r.status_code == 200, r.text
        a = mongo.pamm_strategy_assignments.find_one(
            {"pamm_program_id": prog})
        assert a["min_weight"] == 0.0
        assert a["target_weight"] == 1.0
        assert a["max_weight"] == 1.0

    def test_patch_min_weight_400(self, admin_session, prog):
        r = admin_session.patch(
            f"{BASE_URL}/api/pamm/programs/{prog}/strategy",
            json={"min_weight": 0.1},
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "weights_fixed_in_single"

    def test_patch_target_weight_400(self, admin_session, prog):
        r = admin_session.patch(
            f"{BASE_URL}/api/pamm/programs/{prog}/strategy",
            json={"target_weight": 0.5},
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "weights_fixed_in_single"

    def test_patch_max_weight_400(self, admin_session, prog):
        r = admin_session.patch(
            f"{BASE_URL}/api/pamm/programs/{prog}/strategy",
            json={"max_weight": 0.9},
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "weights_fixed_in_single"


# ═════ 3. Validation policy + account-scoped ═══════════════════════════

class TestValidationPolicy:
    def _validate(self, admin_session, mongo, strategy_id, expected_policy):
        pid = _create_program(admin_session, f"val_{strategy_id}")
        try:
            admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"mode": "SINGLE", "strategy_id": strategy_id,
                      "risk_profile_id": "controlled"},
                headers=_csrf(admin_session), timeout=15)
            r = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy/validate",
                headers=_csrf(admin_session), timeout=20)
            assert r.status_code == 200, r.text
            j = r.json()
            checks = {c["key"]: c for c in j["checks"]}
            assert "execution_eligibility" in checks, \
                f"{strategy_id} must include execution_eligibility"
            elig = checks["execution_eligibility"]
            assert elig["detail"]["policy"] == expected_policy, \
                f"{strategy_id} policy expected {expected_policy}, got {elig['detail']}"
            assert "account_scoped" in elig["detail"]
            assert isinstance(elig["detail"]["account_scoped"], bool)
        finally:
            _cleanup_program(mongo, pid)

    def test_fast_scalp_policy_is_HIGH(self, admin_session, mongo):
        self._validate(admin_session, mongo, "fast_scalp", "HIGH")

    def test_scalper_policy_is_MODERATE(self, admin_session, mongo):
        self._validate(admin_session, mongo, "scalper", "MODERATE")

    def test_sniper_has_no_execution_eligibility(self, admin_session,
                                                 mongo):
        pid = _create_program(admin_session, "val_sniper")
        try:
            admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"mode": "SINGLE", "strategy_id": "sniper",
                      "risk_profile_id": "controlled"},
                headers=_csrf(admin_session), timeout=15)
            r = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy/validate",
                headers=_csrf(admin_session), timeout=20)
            assert r.status_code == 200, r.text
            keys = {c["key"] for c in r.json()["checks"]}
            assert "execution_eligibility" not in keys, \
                f"sniper should NOT have execution_eligibility, got {keys}"
        finally:
            _cleanup_program(mongo, pid)


# ═════ 4. LIVE activation enforcement ═══════════════════════════════════

class TestLiveActivation:
    def test_live_master_requires_certification_then_demo_allows(
            self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "liveact")
        acc_id = _insert_paper_account(mongo, admin_id, broker_env="LIVE")
        _bind_master(mongo, pid, acc_id)
        try:
            # assign sniper (pamm-eligible, non-Nitro)
            admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"mode": "SINGLE", "strategy_id": "sniper",
                      "risk_profile_id": "controlled"},
                headers=_csrf(admin_session), timeout=15)
            admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy/validate",
                headers=_csrf(admin_session), timeout=20)
            # LIVE account -> certification_required 409
            r = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy/activate",
                headers=_headers(admin_session, step_up=True), timeout=15)
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["error"] == "certification_required"
            # Flip account to DEMO -> activation succeeds
            mongo.accounts.update_one(
                {"_id": ObjectId(acc_id)},
                {"$set": {"broker_environment": "DEMO"}})
            r = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy/activate",
                headers=_headers(admin_session, step_up=True), timeout=15)
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "ACTIVE"
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)


# ═════ 5. Execution guard E2E via /api/trades/manual ═══════════════════

class TestExecutionGuardE2E:
    def _fire(self, admin_session, account_id, symbol="EURUSD",
              lot_size=0.01):
        return admin_session.post(
            f"{BASE_URL}/api/trades/manual",
            json={"account_id": account_id, "symbol": symbol,
                  "action": "BUY", "lot_size": lot_size,
                  "sl_pips": 20, "tp1_pips": 10, "tp2_pips": 20,
                  "tp3_pips": 30},
            headers=_csrf(admin_session), timeout=25)

    def test_scenario_A_legacy_passthrough(self, admin_session, admin_id,
                                           mongo):
        """Program with master_account_id set but NO strategy assignment
        → LEGACY mode: manual trade proceeds as before."""
        pid = _create_program(admin_session, "gA")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            r = self._fire(admin_session, acc_id)
            # Trade may succeed (200 with trade doc) or be blocked by
            # market_closed depending on hours, but MUST NOT be blocked
            # by the strategy guard.
            body = r.json() if r.status_code < 500 else {}
            assert r.status_code == 200, f"{r.status_code}: {r.text}"
            if isinstance(body, dict) and body.get("blocked"):
                assert body["blocked"] != "pamm_strategy_guard", body
                # market_closed etc are fine
            else:
                # a real trade doc — should have NO pamm_strategy_id
                assert not body.get("pamm_strategy_id"), body
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)

    def test_scenario_B_strategy_mode_not_active_blocked_HTTP(
            self, admin_session, admin_id, mongo):
        """v62.4: a manual paper trade on a strategy-GOVERNED program is
        the DEDICATED manual-override path — allowed with step-up MFA
        (conftest auto-injects the bypass), explicit override origin, and
        NEVER attributed to the assigned strategy. Without step-up it is
        refused outright."""
        pid = _create_program(admin_session, "gB")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"mode": "SINGLE", "strategy_id": "sniper",
                      "risk_profile_id": "controlled"},
                headers=_csrf(admin_session), timeout=15)
            # 1) without step-up → 403 (override REQUIRES fresh MFA)
            prev = admin_session.headers.get("X-Step-Up-Bypass")
            admin_session.headers["X-Step-Up-Bypass"] = ""
            try:
                r = self._fire(admin_session, acc_id)
            finally:
                if prev is None:
                    admin_session.headers.pop("X-Step-Up-Bypass", None)
                else:
                    admin_session.headers["X-Step-Up-Bypass"] = prev
            assert r.status_code in (401, 403), r.text
            # 2) with step-up → ALLOWED as manual override, program-owned,
            #    NOT attributed to sniper
            r = self._fire(admin_session, acc_id)
            assert r.status_code == 200, r.text
            body = r.json()
            if body.get("blocked") == "market_closed":
                pytest.skip("weekend market close — manual order cannot "
                            "execute (safety gate working as designed)")
            assert not body.get("blocked"), body
            assert body.get("pamm_manual_override") is True, body
            assert body.get("pamm_program_id") == pid, body
            assert not body.get("pamm_strategy_id"), body
            assert body.get("origin") == "manual_override", body
        finally:
            mongo.trades.delete_many({"account_id": acc_id})
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)

    def test_scenario_B_direct_guard_rejects_assignment_not_active(
            self, admin_session, admin_id, mongo):
        """Direct guard invocation (motor) — validates the guard logic
        independent of the PaperEngine bypass bug. This must pass."""
        import asyncio

        from motor.motor_asyncio import AsyncIOMotorClient
        pid = _create_program(admin_session, "gBd")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"mode": "SINGLE", "strategy_id": "sniper",
                      "risk_profile_id": "controlled"},
                headers=_csrf(admin_session), timeout=15)

            async def _run():
                import sys
                from pathlib import Path
                sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
                from modules.pamm.strategy_guard import (
                    authorize_pamm_strategy_execution, resolve_program)
                mc = AsyncIOMotorClient(MONGO_URL)
                db = mc[DB_NAME]
                try:
                    prog = await resolve_program(
                        db, acc_id, {"symbol": "EURUSD"})
                    assert prog is not None, \
                        "guard.resolve_program must find program by " \
                        "master_account_id"
                    acc = await db.accounts.find_one(
                        {"_id": ObjectId(acc_id)})
                    out = await authorize_pamm_strategy_execution(
                        db, prog, acc,
                        {"symbol": "EURUSD", "action": "BUY",
                         "lot_size": 0.01})
                    return out
                finally:
                    mc.close()

            out = asyncio.new_event_loop().run_until_complete(_run())
            assert out["authorized"] is False, out
            assert out["reason"] == "assignment_not_active", out
            assert out["mode"] == "STRATEGY", out
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)

    def test_scenario_C_active_stamps_pamm_identity_direct(
            self, admin_session, admin_id, mongo):
        """Direct guard call — validates that with an ACTIVE assignment
        the guard AUTHORIZES and returns full PAMM lineage context
        (pamm_program_id, assignment_id, strategy_id, strategy_version,
        strategy_hash, risk_profile_id, certification_id)."""
        import asyncio

        from motor.motor_asyncio import AsyncIOMotorClient
        pid = _create_program(admin_session, "gC")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"mode": "SINGLE", "strategy_id": "sniper",
                      "risk_profile_id": "controlled"},
                headers=_csrf(admin_session), timeout=15)
            admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy/validate",
                headers=_csrf(admin_session), timeout=20)
            ar = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy/activate",
                headers=_headers(admin_session, step_up=True), timeout=15)
            assert ar.status_code == 200, ar.text

            async def _run():
                import sys
                from pathlib import Path
                sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
                from modules.pamm.strategy_guard import (
                    authorize_pamm_strategy_execution, resolve_program)
                mc = AsyncIOMotorClient(MONGO_URL)
                db = mc[DB_NAME]
                try:
                    prog = await resolve_program(
                        db, acc_id, {"symbol": "EURUSD"})
                    acc = await db.accounts.find_one(
                        {"_id": ObjectId(acc_id)})
                    return await authorize_pamm_strategy_execution(
                        db, prog, acc,
                        {"symbol": "EURUSD", "action": "BUY",
                         "strategy_id": "sniper", "lot_size": 0.01})
                finally:
                    mc.close()

            out = asyncio.new_event_loop().run_until_complete(_run())
            assert out["authorized"] is True, out
            assert out["mode"] == "STRATEGY", out
            ctx = out["context"]
            assert ctx["pamm_program_id"] == pid, ctx
            assert ctx["strategy_id"] == "sniper", ctx
            assert ctx["risk_profile_id"] == "controlled", ctx
            for k in ("assignment_id", "strategy_version", "strategy_hash"):
                assert ctx.get(k), f"missing {k} in ctx: {ctx}"
            assert "certification_id" in ctx
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)

    def test_scenario_D_max_open_positions_blocks_direct(
            self, admin_session, admin_id, mongo):
        """Direct guard call — with conservative profile (max=2) and 2
        pre-existing open trades on the master account, guard MUST reject
        reason=max_open_positions_reached."""
        import asyncio

        from motor.motor_asyncio import AsyncIOMotorClient
        pid = _create_program(admin_session, "gD")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"mode": "SINGLE", "strategy_id": "sniper",
                      "risk_profile_id": "conservative"},
                headers=_csrf(admin_session), timeout=15)
            admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy/validate",
                headers=_csrf(admin_session), timeout=20)
            ar = admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy/activate",
                headers=_headers(admin_session, step_up=True), timeout=15)
            assert ar.status_code == 200, ar.text
            for _ in range(2):
                mongo.trades.insert_one({
                    "trade_id": f"iter218_open_{uuid.uuid4().hex[:8]}",
                    "account_id": acc_id, "status": "open",
                    "symbol": "EURUSD"})

            async def _run():
                import sys
                from pathlib import Path
                sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
                from modules.pamm.strategy_guard import (
                    authorize_pamm_strategy_execution, resolve_program)
                mc = AsyncIOMotorClient(MONGO_URL)
                db = mc[DB_NAME]
                try:
                    prog = await resolve_program(
                        db, acc_id, {"symbol": "EURUSD"})
                    acc = await db.accounts.find_one(
                        {"_id": ObjectId(acc_id)})
                    return await authorize_pamm_strategy_execution(
                        db, prog, acc,
                        {"symbol": "EURUSD", "action": "BUY",
                         "strategy_id": "sniper", "lot_size": 0.01})
                finally:
                    mc.close()

            out = asyncio.new_event_loop().run_until_complete(_run())
            assert out["authorized"] is False, out
            assert out["reason"] == "max_open_positions_reached", out
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)


# ═════ 6. Strategy ownership endpoint ══════════════════════════════════

class TestStrategyOwnership:
    def test_no_assignment_untagged_healthy(
            self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "ownL")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            # untagged open trade on the master → LEGACY_UNTAGGED
            mongo.trades.insert_one({
                "trade_id": f"iter218_leg_{uuid.uuid4().hex[:8]}",
                "account_id": acc_id, "status": "open",
                "symbol": "EURUSD"})
            r = admin_session.get(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy-ownership",
                timeout=15)
            assert r.status_code == 200, r.text
            j = r.json()
            assert j["counts"]["LEGACY_UNTAGGED"] >= 1
            assert j["healthy"] is True
            assert j["flagged"] == []
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)

    def test_active_untagged_flagged_unhealthy(
            self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "ownA")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"mode": "SINGLE", "strategy_id": "sniper",
                      "risk_profile_id": "controlled"},
                headers=_csrf(admin_session), timeout=15)
            admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy/validate",
                headers=_csrf(admin_session), timeout=20)
            admin_session.post(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy/activate",
                headers=_headers(admin_session, step_up=True), timeout=15)
            # untagged open trade on the master + ACTIVE assignment
            mongo.trades.insert_one({
                "trade_id": f"iter218_pou_{uuid.uuid4().hex[:8]}",
                "account_id": acc_id, "status": "open",
                "symbol": "EURUSD"})
            r = admin_session.get(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy-ownership",
                timeout=15)
            assert r.status_code == 200, r.text
            j = r.json()
            assert j["counts"]["PAMM_OWNER_UNKNOWN"] >= 1
            assert j["healthy"] is False
            assert any(f["classification"] == "PAMM_OWNER_UNKNOWN"
                       for f in j["flagged"])
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)

    def test_bola_non_member_forbidden(
            self, admin_session, plain_user, mongo):
        pid = _create_program(admin_session, "ownB")
        try:
            r = plain_user["session"].get(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy-ownership",
                timeout=15)
            assert r.status_code in (403, 404), r.text
        finally:
            _cleanup_program(mongo, pid)


# ═════ 7. Regression ═══════════════════════════════════════════════════

class TestRegression:
    def test_feature_flag_require_certification_true(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/pamm/strategies", timeout=15)
        assert r.status_code == 200
        ff = r.json()["feature_flags"]
        assert ff.get("PAMM_REQUIRE_CERTIFICATION") is True

    def test_non_pamm_manual_trade_unaffected(
            self, admin_session, admin_id, mongo):
        """A regular paper account with NO program pointing at it must
        execute completely unaffected — no guard invocation, no pamm_*
        fields on the trade."""
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        try:
            r = admin_session.post(
                f"{BASE_URL}/api/trades/manual",
                json={"account_id": acc_id, "symbol": "EURUSD",
                      "action": "BUY", "lot_size": 0.01,
                      "sl_pips": 20, "tp1_pips": 10, "tp2_pips": 20,
                      "tp3_pips": 30},
                headers=_csrf(admin_session), timeout=25)
            assert r.status_code == 200, r.text
            body = r.json()
            if isinstance(body, dict) and body.get("blocked"):
                # only market_closed etc allowed — never guard
                assert body["blocked"] != "pamm_strategy_guard", body
            else:
                assert not body.get("pamm_program_id"), body
                assert not body.get("pamm_strategy_id"), body
        finally:
            _cleanup_account(mongo, acc_id)
