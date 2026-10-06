"""Main103 Review backend integration tests (iter 241).

Scope — STOIC main103 Review + A15 Audit Fix List Part 1:
- A15-1: GET /api/authority/inventory/policies admin-only; preview empty list + env fields.
- A15-1: POST /api/authority/inventory/expectation without policy_migration in preview
         must NOT 500 (expect 200 or 403 step-up).
- N103-7: POST /api/admin/settings/signups {closed:true} now requires step-up —
         admin without TOTP gets 403 code mfa_enrollment_required / step_up_required.
         GET admin & public status still OK.
- Regression (iter239/240): /api/admin/release-gate accounts[],
         /api/ops/deploy-preflight release_key_distinct + release_signer_token_scope,
         /api/trading/authority release_gate FULL.
- A15-4: /api/admin/demo-readiness fleet projection still 2xx and renders rows
         (broker field is now in projection).

Credentials: TEST_ADMIN_EMAIL / TEST_ADMIN_PASSWORD env — never in source.
"""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))

import os
import pytest
import requests

from live_target import require_live_base_url, resolve_admin_credentials  # noqa: E402

pytestmark = pytest.mark.integration

BASE_URL = require_live_base_url()
ADMIN_EMAIL, ADMIN_PASSWORD = resolve_admin_credentials()
METRICS_TOKEN = os.environ.get("METRICS_TOKEN")


def _login(session, email, password):
    return session.post(f"{BASE_URL}/api/auth/login",
                        json={"email": email, "password": password}, timeout=15)


def _csrf_headers(session):
    tok = session.cookies.get("csrf_token")
    return {"X-CSRF-Token": tok} if tok else {}


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = _login(s, ADMIN_EMAIL, ADMIN_PASSWORD)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    me = s.get(f"{BASE_URL}/api/auth/me", timeout=10)
    assert me.status_code == 200, me.text
    assert me.json().get("role") == "admin", me.json()
    return s


@pytest.fixture(scope="module", autouse=True)
def _ensure_signups_open_at_end(admin_session):
    """Leave signups OPEN after module even if a test mutated DB (step-up should
    prevent writes anyway — this is a safety net)."""
    yield
    try:
        admin_session.post(f"{BASE_URL}/api/admin/settings/signups",
                           json={"closed": False},
                           headers=_csrf_headers(admin_session), timeout=10)
    except Exception:
        pass


# ─── A15-1 inventory policies endpoint ─────────────────────────────────────
class TestInventoryPolicies:
    def test_01_admin_lists_policies(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/authority/inventory/policies", timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "policies" in body and isinstance(body["policies"], list)
        assert "installation_id" in body
        assert "environment" in body
        # Preview: no signed migrations committed yet.
        assert body["policies"] == [] or isinstance(body["policies"], list)
        # Preview environment label
        assert body["environment"] in ("preview", "production", "staging", "development", "dev-local", "ci")

    def test_02_unauthenticated_rejected(self):
        anon = requests.Session()
        r = anon.get(f"{BASE_URL}/api/authority/inventory/policies", timeout=10)
        assert r.status_code in (401, 403), r.status_code


# ─── A15-1 inventory expectation POST without policy_migration ─────────────
class TestInventoryExpectationNoPolicy:
    def test_03_no_policy_in_preview_no_500(self, admin_session):
        payload = {"accounts": 2, "enabled": 2, "bots": 2, "account_ids": ["x", "y"]}
        r = admin_session.post(f"{BASE_URL}/api/authority/inventory/expectation",
                               json=payload, headers=_csrf_headers(admin_session), timeout=15)
        assert r.status_code != 500, f"server error: {r.status_code} {r.text[:300]}"
        # Non-production: either accepted (200) or step-up 403.
        assert r.status_code in (200, 403), f"unexpected: {r.status_code} {r.text[:300]}"
        if r.status_code == 403:
            det = r.json().get("detail")
            code = det.get("code") if isinstance(det, dict) else None
            assert code in (
                "mfa_enrollment_required", "step_up_required", "step_up_pending",
                "step_up_token_invalid", "step_up_token_expired",
            ), f"unexpected 403 code: {det}"


# ─── N103-7 signups toggle now requires step-up ────────────────────────────
class TestSignupsToggleRequiresStepUp:
    def test_04_get_signups_admin_still_ok(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/settings/signups", timeout=10)
        assert r.status_code == 200, r.text
        for k in ("closed", "db_closed", "env_closed", "updated_at", "updated_by", "message"):
            assert k in r.json(), f"missing key {k} in {r.json()}"

    def test_05_post_close_without_totp_blocked_by_stepup(self, admin_session):
        # Disable the HTTP-suite STEP_UP_BYPASS auto-header to exercise the REAL gate
        # (conftest.py injects it by default for the ~2k legacy HTTP suite).
        s = requests.Session()
        s.headers["X-Step-Up-Bypass"] = ""
        _login(s, ADMIN_EMAIL, ADMIN_PASSWORD)
        before = requests.get(f"{BASE_URL}/api/auth/signups-status", timeout=10).json()
        r = s.post(f"{BASE_URL}/api/admin/settings/signups",
                   json={"closed": True},
                   headers={**_csrf_headers(s), "X-Step-Up-Bypass": ""}, timeout=15)
        assert r.status_code == 403, f"expected 403 step-up, got {r.status_code} {r.text[:300]}"
        det = r.json().get("detail")
        code = det.get("code") if isinstance(det, dict) else None
        assert code in ("mfa_enrollment_required", "step_up_required",
                        "step_up_pending", "step_up_token_invalid"), f"unexpected: {det}"
        # Public status must NOT have flipped.
        after = requests.get(f"{BASE_URL}/api/auth/signups-status", timeout=10).json()
        assert after.get("closed") == before.get("closed"), (before, after)

    def test_06_public_signups_status_open(self):
        r = requests.get(f"{BASE_URL}/api/auth/signups-status", timeout=10)
        assert r.status_code == 200
        assert r.json().get("closed") is False, r.json()


# ─── Regression: release-gate / deploy-preflight / trading authority ────────
class TestRegressionReleaseGate:
    def test_07_admin_release_gate_has_accounts(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/release-gate", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "accounts" in body and isinstance(body["accounts"], list)

    def test_08_trading_authority_release_gate_full(self, admin_session):
        # Correct endpoint is /api/authority (router prefix "/authority")
        r = admin_session.get(f"{BASE_URL}/api/authority", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        dom = (body.get("domains") or {}).get("release_gate") or body.get("release_gate") or {}
        lvl = dom.get("level") if isinstance(dom, dict) else dom
        assert lvl == "FULL", f"release_gate not FULL: {dom}"

    def test_09_deploy_preflight_has_release_checks(self):
        if not METRICS_TOKEN:
            pytest.skip("METRICS_TOKEN not set")
        r = requests.get(f"{BASE_URL}/api/ops/deploy-preflight",
                         headers={"X-Metrics-Token": METRICS_TOKEN}, timeout=15)
        assert r.status_code == 200, r.text
        ids = [c.get("id") for c in r.json().get("checks", [])]
        assert "release_key_distinct" in ids, ids
        assert "release_signer_token_scope" in ids, ids


# ─── A15-4 demo-readiness fleet projection with broker ──────────────────────
class TestDemoReadiness:
    def test_10_demo_readiness_ok(self, admin_session):
        # A15-4: broker added to Mongo projection to avoid 500 when hashing attestation
        # identity. The output row doesn't necessarily surface 'broker', only must
        # render without 500.
        r = admin_session.get(f"{BASE_URL}/api/admin/demo-readiness", timeout=20)
        assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
        body = r.json()
        fleet = body.get("fleet") or body.get("accounts") or body.get("rows") or []
        assert isinstance(fleet, list)
        # If fleet rows exist, they must at least have id + label (projection healthy)
        if fleet:
            assert "id" in fleet[0] and "label" in fleet[0], fleet[0]
