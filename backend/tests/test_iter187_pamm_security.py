"""iter-187 — PAMM security audit fixes (SEC-001/002/003):
step-up MFA on risk-increasing PAMM mutations, rate limiting on PAMM
POSTs, sanitized error messages on broker-derived failures."""
import os
import sys
import uuid

import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"
TIMEOUT = 25


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _admin(real_step_up=False):
    s = requests.Session()
    if real_step_up:  # disable the conftest bypass → exercise the REAL gate
        s.headers["X-Step-Up-Bypass"] = ""
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


def _create_program(s, name):
    r = s.post(f"{API}/pamm/programs", json={"name": name}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()


def _cleanup(program_id):
    db = _db()
    p = _run(db.pamm_programs.find_one({"program_id": program_id})) or {}
    bpid = p.get("broker_program_id")
    for c in ("pamm_programs", "pamm_master_accounts", "pamm_allocations",
              "pamm_nav_snapshots", "pamm_reconciliation", "pamm_audit"):
        _run(getattr(db, c).delete_many({"program_id": program_id}))
    if bpid:
        for c in ("sandbox_broker_programs", "sandbox_broker_investors",
                  "sandbox_broker_allocations", "sandbox_broker_positions"):
            _run(getattr(db, c).delete_many({"program_id": bpid}))


def _assert_step_up_403(r):
    assert r.status_code == 403, f"{r.status_code}: {r.text}"
    detail = r.json().get("detail")
    assert isinstance(detail, dict), r.text
    assert detail.get("code") in ("step_up_required",
                                  "mfa_enrollment_required"), detail
    assert detail.get("action") == "risk_raise"


class TestStepUpGates:
    """SEC-001 — risk-increasing PAMM mutations demand fresh step-up MFA."""

    def test_gated_endpoints_403_without_step_up(self):
        s_bypass = _admin()  # bypass ON → allowed to create fixture
        prog = _create_program(s_bypass, f"sec-su-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        s = _admin(real_step_up=True)
        try:
            # 1. weaken risk limits
            r = s.put(f"{API}/pamm/programs/{pid}/risk-limits",
                      json={"daily_loss_pct": {"threshold": 99.0}},
                      timeout=TIMEOUT)
            _assert_step_up_403(r)
            # 2. clear risk breach
            r = s.post(f"{API}/pamm/programs/{pid}/clear-risk-breach",
                       timeout=TIMEOUT)
            _assert_step_up_403(r)
            # 3. clear emergency stop
            r = s.post(f"{API}/pamm/programs/{pid}/clear-emergency-stop",
                       timeout=TIMEOUT)
            _assert_step_up_403(r)
            # 4. resume trading (pause first via bypass session)
            rr = s_bypass.post(f"{API}/pamm/programs/{pid}/pause",
                               json={"reason": "test"}, timeout=TIMEOUT)
            assert rr.status_code == 200, rr.text
            r = s.post(f"{API}/pamm/programs/{pid}/resume", timeout=TIMEOUT)
            _assert_step_up_403(r)
            # 5. manager role grant
            r = s.post(f"{API}/pamm/managers",
                       json={"user_id": "0" * 24, "grant": True},
                       timeout=TIMEOUT)
            _assert_step_up_403(r)
            # nothing actually changed
            p = _run(_db().pamm_programs.find_one({"program_id": pid}))
            assert p["trading"] == "paused"
            assert p["risk_limits"]["daily_loss_pct"]["threshold"] != 99.0 \
                if p.get("risk_limits") else True
        finally:
            _cleanup(pid)

    def test_gated_endpoints_work_with_step_up_satisfied(self):
        """Conftest injects the (non-production) step-up bypass → the same
        endpoints succeed, proving the gate is the only blocker."""
        s = _admin()
        prog = _create_program(s, f"sec-su-ok-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        try:
            r = s.put(f"{API}/pamm/programs/{pid}/risk-limits",
                      json={"daily_loss_pct": {"threshold": 4.0}},
                      timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            s.post(f"{API}/pamm/programs/{pid}/pause",
                   json={"reason": "t"}, timeout=TIMEOUT)
            r = s.post(f"{API}/pamm/programs/{pid}/resume", timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            r = s.post(f"{API}/pamm/programs/{pid}/clear-risk-breach",
                       timeout=TIMEOUT)
            assert r.status_code == 200, r.text
        finally:
            _cleanup(pid)

    def test_pause_and_estop_do_not_require_step_up(self):
        """Risk-REDUCING actions must stay instantly available."""
        s_bypass = _admin()
        prog = _create_program(s_bypass, f"sec-pz-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        s = _admin(real_step_up=True)
        try:
            r = s.post(f"{API}/pamm/programs/{pid}/pause",
                       json={"reason": "halt now"}, timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            r = s.post(f"{API}/pamm/programs/{pid}/emergency-stop",
                       json={"reason": "kill"}, timeout=TIMEOUT)
            assert r.status_code == 200, r.text
        finally:
            _cleanup(pid)


class TestRateLimits:
    """SEC-002 — PAMM POSTs are rate limited (health/check: 10/min)."""

    def test_health_check_rate_limited(self):
        s = _admin()
        got_429 = False
        try:
            for _ in range(12):
                r = s.post(f"{API}/pamm/health/check",
                           headers={"X-RateLimit-Bypass": ""},
                           timeout=TIMEOUT)
                if r.status_code == 429:
                    got_429 = True
                    break
                assert r.status_code == 200, r.text
            assert got_429, "expected 429 within 12 rapid health checks"
        finally:  # clean the fixed window so later tests aren't throttled
            db = _db()
            _run(db.rate_limits.delete_many(
                {"_id": {"$regex": "^pamm_"}}))

    def test_webhook_endpoint_rate_limited_per_partner(self):
        """Pre-seed the fixed-window counter to the cap → next hit is 429."""
        from datetime import datetime, timezone
        fake_partner = f"prt_ghost_{uuid.uuid4().hex[:6]}"
        now = datetime.now(timezone.utc)
        window_start = int(now.timestamp()) // 60 * 60
        key = f"pamm_webhook:{fake_partner}:{window_start}"
        _run(_db().rate_limits.insert_one({"_id": key, "n": 600}))
        try:
            r = requests.post(f"{API}/pamm/webhooks/{fake_partner}",
                              json={}, timeout=TIMEOUT,
                              headers={"X-RateLimit-Bypass": ""})
            assert r.status_code == 429, f"{r.status_code}: {r.text}"
        finally:
            _run(_db().rate_limits.delete_many(
                {"_id": {"$regex": "^pamm_webhook"}}))


class TestSanitizedErrors:
    """SEC-003 — broker-derived failures return generic messages."""

    def test_investor_error_is_generic(self):
        s = _admin()
        prog = _create_program(s, f"sec-err-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        try:
            r = s.post(f"{API}/pamm/programs/{pid}/investors",
                       json={"name": "x", "email": "x@y.z", "amount": -50},
                       timeout=TIMEOUT)
            assert r.status_code == 400, r.text
            detail = r.json()["detail"]
            assert "broker:" not in detail  # no internal adapter text
            assert "Allocation rejected" in detail
        finally:
            _cleanup(pid)

    def test_webhook_errors_are_generic(self):
        # unknown partner / bad signature → generic strings only
        r = requests.post(f"{API}/pamm/webhooks/prt_sandbox", json={},
                          timeout=TIMEOUT)
        assert r.status_code in (400, 401), r.text
        assert r.json()["detail"] in ("unauthorized", "invalid webhook")
        r = requests.post(f"{API}/pamm/webhooks/prt_does_not_exist",
                          json={}, timeout=TIMEOUT)
        assert r.status_code in (400, 401), r.text
        detail = r.json()["detail"]
        assert "unknown broker partner" not in detail
        assert detail in ("unauthorized", "invalid webhook")

    def test_create_program_error_is_generic(self):
        """Non-sandbox partner without broker-side program → generic 400."""
        db = _db()
        fake = {"partner_id": f"prt_fake_{uuid.uuid4().hex[:6]}",
                "name": "FakeBroker", "adapter": "sandbox",
                "status": "active"}
        _run(db.broker_partners.insert_one(dict(fake)))
        s = _admin()
        try:
            r = s.post(f"{API}/pamm/programs",
                       json={"name": f"nope-{uuid.uuid4().hex[:4]}",
                             "partner_id": fake["partner_id"]},
                       timeout=TIMEOUT)
            # sandbox adapter auto-provisions only for prt_sandbox
            assert r.status_code == 400, r.text
            detail = r.json()["detail"]
            assert "Program creation failed" in detail
            assert "not found on broker" not in detail
        finally:
            _run(db.broker_partners.delete_many(
                {"partner_id": fake["partner_id"]}))
            _run(db.pamm_health.delete_many(
                {"partner_id": fake["partner_id"]}))


class TestAuditTrail:
    def test_step_up_mutations_are_audited(self):
        s = _admin()
        prog = _create_program(s, f"sec-aud-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        try:
            r = s.put(f"{API}/pamm/programs/{pid}/risk-limits",
                      json={"weekly_loss_pct": {"threshold": 8.0}},
                      timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            entry = _run(_db().audit_log.find_one(
                {"action": "pamm_risk_limits_update",
                 "detail.program_id": pid}))
            assert entry is not None
        finally:
            _run(_db().audit_log.delete_many(
                {"detail.program_id": pid}))
            _cleanup(pid)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
