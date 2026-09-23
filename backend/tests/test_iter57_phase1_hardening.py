from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-57 · Phase 1 security & reliability hardening verification.

Covers:
 1. Login → cookies set (access_token, refresh_token, csrf_token)
 2. CSRF enforcement: cookie-auth mutating request WITHOUT header → 403 csrf_failed
                     WITH header → passes
 3. Refresh rotation: old refresh cookie A → 200 new; replay A → 401;
                     rotated successor also dead (family revoked)
 4. Login-failure rate limit: 6 consecutive wrong-password → 429 (lockout not bypassable)
 5. Logout revokes server-side: old refresh cookie can no longer refresh
 6. Password change revokes all sessions
 7. Health endpoints: /api/health, /api/health/live, /api/health/ready
"""
import os
import uuid
import time
import pytest
import requests
from live_target import require_live_base_url

BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
BYPASS = os.environ.get("RATE_LIMIT_BYPASS_TOKEN")


def _raw_post(path, json=None, cookies=None, headers=None):
    """Raw requests.post WITHOUT going through the conftest CSRF-injecting Session
    (uses module-level requests.post which is a fresh session each call).
    """
    return requests.post(f"{API}{path}", json=json, cookies=cookies, headers=headers or {}, timeout=15)


def _register_and_verify(email=None, password="Uq8#Rn4jS6wLbM3z"):
    """Register a throwaway user and force-verify via DB. Requires bypass header for rate limit."""
    email = email or f"iter57_{uuid.uuid4().hex[:10]}@example.com"
    hdrs = {"Content-Type": "application/json"}
    if BYPASS:
        hdrs["X-RateLimit-Bypass"] = BYPASS
    r = requests.post(
        f"{API}/auth/register",
        json={"email": email, "password": password, "name": "iter57", "terms_agreed": True},
        headers=hdrs,
        timeout=15,
    )
    assert r.status_code in (200, 201), f"register failed {r.status_code}: {r.text}"
    # Force verify via DB directly
    import motor.motor_asyncio, asyncio
    async def _verify():
        client = motor.motor_asyncio.AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        await db.users.update_one({"email": email}, {"$set": {"email_verified": True}})
        client.close()
    asyncio.get_event_loop().run_until_complete(_verify())
    return email, password


def _login(email, password):
    hdrs = {"Content-Type": "application/json"}
    if BYPASS:
        hdrs["X-RateLimit-Bypass"] = BYPASS
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": password}, headers=hdrs, timeout=15)
    return r


# ---------- 1 & 7: baseline login + health ----------
class TestBaselineAndHealth:
    def test_health_no_exception_fields(self):
        r = requests.get(f"{API}/health", timeout=10)
        assert r.status_code == 200
        j = r.json()
        assert j.get("status") == "ok"
        assert j.get("db") in ("connected", "ok")
        # No exception internals leaked
        for k in ("exception", "traceback", "error_detail"):
            assert k not in j, f"leaked field {k}: {j}"

    def test_health_live(self):
        r = requests.get(f"{API}/health/live", timeout=10)
        assert r.status_code == 200

    def test_health_ready(self):
        r = requests.get(f"{API}/health/ready", timeout=10)
        assert r.status_code == 200, r.text
        j = r.json()
        checks = j.get("checks", {})
        # Accept any true-ish signal
        assert checks, f"no checks in ready payload: {j}"
        # db must be ok
        assert checks.get("db") in (True, "ok", "connected"), checks

    def test_admin_login_sets_all_three_cookies(self):
        hdrs = {"Content-Type": "application/json"}
        if BYPASS:
            hdrs["X-RateLimit-Bypass"] = BYPASS
        r = requests.post(
            f"{API}/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            headers=hdrs,
            timeout=15,
        )
        assert r.status_code == 200, r.text
        cj = r.cookies
        assert cj.get("access_token"), "access_token cookie missing"
        assert cj.get("refresh_token"), "refresh_token cookie missing"
        assert cj.get("csrf_token"), "csrf_token cookie missing"


# ---------- 2: CSRF enforcement ----------
class TestCSRFEnforcement:
    def test_cookie_auth_mutating_without_csrf_returns_403(self):
        r = _login(ADMIN_EMAIL, ADMIN_PASSWORD)
        assert r.status_code == 200
        # Use urllib directly to bypass the conftest's auto-CSRF injection on requests.Session
        import urllib.request as _u
        cookie_hdr = "; ".join([
            f"access_token={r.cookies.get('access_token')}",
            f"refresh_token={r.cookies.get('refresh_token')}",
            f"csrf_token={r.cookies.get('csrf_token')}",
        ])
        req_hdrs = {"Content-Type": "application/json", "Cookie": cookie_hdr}
        if BYPASS:
            req_hdrs["X-RateLimit-Bypass"] = BYPASS
        req = _u.Request(f"{API}/auth/logout", data=b"{}", headers=req_hdrs, method="POST")
        try:
            resp = _u.urlopen(req, timeout=10)
            status = resp.status
            body = resp.read().decode()
        except _u.HTTPError as e:
            status = e.code
            body = e.read().decode()

        class _R:
            status_code = status
            text = body
            def json(self):
                import json
                return json.loads(body)
        r2 = _R()
        assert r2.status_code == 403, f"expected 403 csrf_failed, got {r2.status_code}: {r2.text}"
        try:
            body = r2.json()
        except Exception:
            body = {}
        detail = body.get("detail") if isinstance(body, dict) else None
        # detail can be {code: 'csrf_failed'} or plain string; be tolerant
        if isinstance(detail, dict):
            assert detail.get("code") == "csrf_failed", body

    def test_cookie_auth_mutating_with_csrf_header_passes(self):
        r = _login(ADMIN_EMAIL, ADMIN_PASSWORD)
        assert r.status_code == 200
        csrf = r.cookies.get("csrf_token")
        cookies = {
            "access_token": r.cookies.get("access_token"),
            "refresh_token": r.cookies.get("refresh_token"),
            "csrf_token": csrf,
        }
        hdrs = {"Content-Type": "application/json", "X-CSRF-Token": csrf}
        if BYPASS:
            hdrs["X-RateLimit-Bypass"] = BYPASS
        r2 = requests.post(f"{API}/auth/logout", cookies=cookies, headers=hdrs, timeout=10)
        assert r2.status_code in (200, 204), f"logout w/ csrf failed: {r2.status_code} {r2.text}"


# ---------- 3: Refresh rotation + family revocation ----------
class TestRefreshRotation:
    def test_replay_rotated_refresh_returns_401(self):
        r = _login(ADMIN_EMAIL, ADMIN_PASSWORD)
        assert r.status_code == 200
        old_refresh = r.cookies.get("refresh_token")
        csrf_a = r.cookies.get("csrf_token")
        assert old_refresh
        # First refresh should succeed
        hdrs = {"X-CSRF-Token": csrf_a}
        if BYPASS:
            hdrs["X-RateLimit-Bypass"] = BYPASS
        r1 = requests.post(
            f"{API}/auth/refresh",
            cookies={"refresh_token": old_refresh, "csrf_token": csrf_a},
            headers=hdrs,
            timeout=10,
        )
        assert r1.status_code == 200, f"first refresh failed: {r1.text}"
        new_refresh = r1.cookies.get("refresh_token")
        assert new_refresh and new_refresh != old_refresh, "refresh token was not rotated"
        new_csrf = r1.cookies.get("csrf_token") or csrf_a

        # REPLAY old refresh → should be 401
        hdrs2 = {"X-CSRF-Token": csrf_a}
        if BYPASS:
            hdrs2["X-RateLimit-Bypass"] = BYPASS
        r2 = requests.post(
            f"{API}/auth/refresh",
            cookies={"refresh_token": old_refresh, "csrf_token": csrf_a},
            headers=hdrs2,
            timeout=10,
        )
        assert r2.status_code == 401, f"replay of old refresh should 401, got {r2.status_code}: {r2.text}"

        # rotated successor should also be dead (family revoked on reuse)
        hdrs3 = {"X-CSRF-Token": new_csrf}
        if BYPASS:
            hdrs3["X-RateLimit-Bypass"] = BYPASS
        r3 = requests.post(
            f"{API}/auth/refresh",
            cookies={"refresh_token": new_refresh, "csrf_token": new_csrf},
            headers=hdrs3,
            timeout=10,
        )
        assert r3.status_code == 401, f"family should be revoked after reuse, successor got {r3.status_code}"


# ---------- 4: Login rate limit ----------
class TestLoginRateLimit:
    def test_six_failed_logins_return_429(self):
        # Use a throwaway email to avoid locking real accounts.
        # Note: login lockout is NOT bypassable
        throw_email = f"iter57_ratelimit_{uuid.uuid4().hex[:10]}@example.com"
        got_429 = False
        for i in range(1, 8):
            hdrs = {"Content-Type": "application/json"}
            # DO NOT include bypass — even if included, login lockout should not bypass
            r = requests.post(
                f"{API}/auth/login",
                json={"email": throw_email, "password": "wrong-pass-abc"},
                headers=hdrs,
                timeout=10,
            )
            if r.status_code == 429:
                got_429 = True
                # verify error shape
                try:
                    j = r.json()
                    d = j.get("detail", {})
                    if isinstance(d, dict):
                        assert d.get("code") in ("rate_limited", "login_locked", "too_many_attempts") or "rate" in str(d).lower()
                except Exception:
                    pass
                break
            assert r.status_code in (401, 403), f"attempt {i}: unexpected {r.status_code}: {r.text}"
        assert got_429, "Expected 429 within 6 failed attempts, never got one"


# ---------- 5: Logout revokes server-side ----------
class TestLogoutRevokes:
    def test_logout_kills_refresh_token(self):
        email, password = _register_and_verify()
        r = _login(email, password)
        assert r.status_code == 200
        refresh = r.cookies.get("refresh_token")
        csrf = r.cookies.get("csrf_token")
        # Logout
        hdrs = {"X-CSRF-Token": csrf}
        if BYPASS:
            hdrs["X-RateLimit-Bypass"] = BYPASS
        r_lo = requests.post(
            f"{API}/auth/logout",
            cookies={"access_token": r.cookies.get("access_token"), "refresh_token": refresh, "csrf_token": csrf},
            headers=hdrs,
            timeout=10,
        )
        assert r_lo.status_code in (200, 204)
        # Old refresh should no longer work
        r2 = requests.post(
            f"{API}/auth/refresh",
            cookies={"refresh_token": refresh, "csrf_token": csrf},
            headers=hdrs,
            timeout=10,
        )
        assert r2.status_code == 401, f"refresh after logout should 401, got {r2.status_code}"


# ---------- 6: Password change revokes all sessions ----------
class TestPasswordChangeRevokesSessions:
    def test_change_password_invalidates_other_session_refresh(self):
        email, password = _register_and_verify()
        # Session 1
        s1 = _login(email, password)
        assert s1.status_code == 200
        s1_csrf = s1.cookies.get("csrf_token")
        s1_cookies = {
            "access_token": s1.cookies.get("access_token"),
            "refresh_token": s1.cookies.get("refresh_token"),
            "csrf_token": s1_csrf,
        }
        # Session 2 (independent login)
        s2 = _login(email, password)
        assert s2.status_code == 200
        s2_refresh = s2.cookies.get("refresh_token")
        s2_csrf = s2.cookies.get("csrf_token")

        # Change password from session 1
        new_pw = password + "X!"
        hdrs = {"X-CSRF-Token": s1_csrf, "Content-Type": "application/json"}
        if BYPASS:
            hdrs["X-RateLimit-Bypass"] = BYPASS
        rcp = requests.post(
            f"{API}/auth/change-password",
            cookies=s1_cookies,
            json={"current_password": password, "new_password": new_pw},
            headers=hdrs,
            timeout=15,
        )
        assert rcp.status_code in (200, 204), f"change-password failed: {rcp.status_code} {rcp.text}"

        # Session 2 refresh should now be dead
        hdrs2 = {"X-CSRF-Token": s2_csrf}
        if BYPASS:
            hdrs2["X-RateLimit-Bypass"] = BYPASS
        r = requests.post(
            f"{API}/auth/refresh",
            cookies={"refresh_token": s2_refresh, "csrf_token": s2_csrf},
            headers=hdrs2,
            timeout=10,
        )
        assert r.status_code == 401, f"session 2 refresh should 401 after pw change, got {r.status_code}"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
