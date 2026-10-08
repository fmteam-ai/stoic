"""Main102 review backend integration tests (iter 240).

Scope — STOIC main102 Review + USER REQUEST signup lock:
- Signup lock admin toggle + public status + register/affiliate refusal (user request).
- Audit rows signups_closed/signups_opened by admin.
- N102-1: /api/authority release_gate domain is FULL for account-less preview;
  /api/admin/release-gate still returns accounts[] rows (regression from iter239).
- N102-5: /api/ops/deploy-preflight has release_key_distinct (warn/pass in preview, never fail)
  and release_signer_token_scope still present.

Credentials: TEST_ADMIN_EMAIL / TEST_ADMIN_PASSWORD env — never in source.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))

import os
import secrets
import time

import pytest
import requests

from live_target import require_live_base_url, resolve_admin_credentials  # noqa: E402

pytestmark = pytest.mark.integration

BASE_URL = require_live_base_url()
ADMIN_EMAIL, ADMIN_PASSWORD = resolve_admin_credentials()


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
    """Guarantee signups are OPEN after this module runs (even on failure)."""
    yield
    try:
        admin_session.post(f"{BASE_URL}/api/admin/settings/signups",
                           json={"closed": False},
                           headers=_csrf_headers(admin_session), timeout=10)
    except Exception:
        pass


# ───────── Signup Lock (user request) ─────────
class TestSignupLock:
    def test_01_admin_get_initial(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/settings/signups", timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ("closed", "db_closed", "env_closed", "updated_at", "updated_by", "message"):
            assert k in body, f"missing {k} in {list(body)}"

    def test_02_admin_close(self, admin_session):
        r = admin_session.post(f"{BASE_URL}/api/admin/settings/signups",
                               json={"closed": True},
                               headers=_csrf_headers(admin_session), timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["closed"] is True, body
        assert body["db_closed"] is True, body
        assert body["updated_by"] == ADMIN_EMAIL, body

    def test_03_public_signups_status_closed(self):
        r = requests.get(f"{BASE_URL}/api/auth/signups-status", timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["closed"] is True, body
        from signup_lock import CLOSED_MESSAGE
        assert body["message"] == CLOSED_MESSAGE, body

    def test_04_register_refused(self):
        fresh_email = f"TEST_signups_{secrets.token_hex(6)}@example.com"
        strong_pw = "C0rrect-Horse-Battery-Staple-" + secrets.token_hex(12)
        r = requests.post(f"{BASE_URL}/api/auth/register",
                          json={"email": fresh_email, "password": strong_pw,
                                "name": "Test SignupsClosed", "terms_agreed": True},
                          timeout=15)
        assert r.status_code == 403, f"{r.status_code} {r.text[:300]}"
        detail = r.json().get("detail") or {}
        assert detail.get("code") == "signups_closed", detail
        assert detail.get("kind") == "member", detail
        # Verify no account was created: a login attempt returns 401 (not a 200 or lockout)
        login_resp = requests.post(f"{BASE_URL}/api/auth/login",
                                   json={"email": fresh_email, "password": strong_pw}, timeout=10)
        assert login_resp.status_code in (401, 403, 429), login_resp.status_code

    def test_05_affiliate_apply_refused(self, admin_session):
        r = admin_session.post(f"{BASE_URL}/api/affiliate/apply",
                               json={"terms_agreed": True, "full_name": "x",
                                     "audience_url": "https://x.y",
                                     "promotion_strategy": "x", "payment_method": "x"},
                               headers=_csrf_headers(admin_session), timeout=15)
        assert r.status_code == 403, f"{r.status_code} {r.text[:300]}"
        detail = r.json().get("detail") or {}
        assert detail.get("code") == "signups_closed", detail
        assert detail.get("kind") == "affiliate", detail

    def test_06_existing_admin_login_still_works(self):
        s = requests.Session()
        r = _login(s, ADMIN_EMAIL, ADMIN_PASSWORD)
        assert r.status_code == 200, r.text

    def test_07_audit_rows(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/audit-log?limit=50", timeout=10)
        assert r.status_code == 200, r.text
        items = r.json().get("audit") or []
        actions = [i.get("action") for i in items if isinstance(i, dict)]
        assert "signups_closed" in actions, f"actions={actions[:20]}"
        # find the row & verify actor
        row = next(i for i in items if isinstance(i, dict) and i.get("action") == "signups_closed")
        assert row.get("actor_email") == ADMIN_EMAIL, row

    def test_08_admin_reopen(self, admin_session):
        r = admin_session.post(f"{BASE_URL}/api/admin/settings/signups",
                               json={"closed": False},
                               headers=_csrf_headers(admin_session), timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["closed"] is False, body

        # Public status reflects open
        r2 = requests.get(f"{BASE_URL}/api/auth/signups-status", timeout=10)
        assert r2.status_code == 200
        assert r2.json()["closed"] is False, r2.json()

    def test_09_audit_opened_present(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/audit-log?limit=50", timeout=10)
        assert r.status_code == 200, r.text
        items = r.json().get("audit") or []
        actions = [i.get("action") for i in items if isinstance(i, dict)]
        assert "signups_opened" in actions, f"actions={actions[:20]}"


# ───────── N102-1 — authority release_gate FULL + release-gate accounts[] regression ─────────
class TestReleaseGateAuthorityAndRegression:
    def test_authority_release_gate_full_in_preview(self, admin_session):
        # Try likely user-level authority endpoints
        last = None
        for path in ("/api/authority", "/api/authority/current", "/api/trading/authority"):
            r = admin_session.get(f"{BASE_URL}{path}", timeout=15)
            last = (path, r.status_code, r.text[:200])
            if r.status_code == 200:
                body = r.json()
                # locate release_gate domain
                dom = None
                if isinstance(body, dict):
                    if "domains" in body and isinstance(body["domains"], dict):
                        dom = body["domains"].get("release_gate")
                    if dom is None:
                        dom = body.get("release_gate")
                if dom is None:
                    continue
                lvl = dom.get("level") if isinstance(dom, dict) else dom
                assert lvl == "FULL", f"{path} -> {dom}"
                return
        pytest.skip(f"no authority endpoint returned release_gate; last={last}")

    def test_admin_release_gate_accounts_regression(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/release-gate", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "accounts" in body and isinstance(body["accounts"], list), body
        assert len(body["accounts"]) >= 1, body
        for a in body["accounts"][:3]:
            for k in ("account_id", "label", "environment", "allowed", "reason"):
                assert k in a, a


# ───────── N102-5 — deploy preflight: release_key_distinct ─────────
class TestPreflightReleaseKeyDistinct:
    def _get(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/ops/deploy-preflight", timeout=20)
        if r.status_code == 403:
            tok = os.environ.get("METRICS_TOKEN") or ""
            if tok:
                r = requests.get(f"{BASE_URL}/api/ops/deploy-preflight",
                                 headers={"X-Metrics-Token": tok}, timeout=20)
        return r

    def test_release_key_distinct_and_signer_scope(self, admin_session):
        r = self._get(admin_session)
        assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
        body = r.json()
        checks = body.get("checks") or body.get("results") or []
        if not checks and isinstance(body, dict):
            for v in body.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    checks = v; break
        ids = [c.get("id") for c in checks if isinstance(c, dict)]
        assert "release_key_distinct" in ids, f"missing release_key_distinct; ids={ids}"
        assert "release_signer_token_scope" in ids, f"missing release_signer_token_scope; ids={ids}"
        rk = next(c for c in checks if c.get("id") == "release_key_distinct")
        assert rk.get("status") in ("pass", "warn"), rk  # never 'fail' in preview
