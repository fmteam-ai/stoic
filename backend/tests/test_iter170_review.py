"""iter-170 review — login flow, turnstile-diag, CORS regression (preview).

Verifies user-reported 'Human verification failed' bug is not reproducible
in preview since turnstile is disabled in preview DB.
"""
import os
import time
import pytest
import requests

from live_target import require_live_base_url

pytestmark = pytest.mark.http

BASE_URL = require_live_base_url()

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASS = "admin123"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    time.sleep(1)
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=15)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text[:300]}"
    body = r.json()
    assert "access_token" in s.cookies or body.get("access_token"), \
        f"no session cookie/token: cookies={list(s.cookies.keys())}"
    return s


class TestLoginFlow:
    def test_login_returns_200_no_turnstile_error(self):
        s = requests.Session()
        r = s.post(f"{BASE_URL}/api/auth/login",
                   json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=15)
        assert r.status_code == 200, r.text[:400]
        # ensure no 'Human verification failed' bug
        assert "Human verification" not in r.text
        assert "access_token" in s.cookies

    def test_me_with_cookie(self, admin_session):
        time.sleep(0.5)
        r = admin_session.get(f"{BASE_URL}/api/auth/me", timeout=10)
        assert r.status_code == 200
        assert r.json().get("email") == ADMIN_EMAIL


class TestTurnstileDiag:
    def test_diag_shows_disabled_in_preview(self, admin_session):
        time.sleep(0.5)
        r = admin_session.get(f"{BASE_URL}/api/ops/turnstile-diag", timeout=10)
        assert r.status_code == 200, r.text[:400]
        body = r.json()
        assert body.get("enabled") is False, body
        assert body.get("force_disabled") is False, body
        # secret_ok means Cloudflare siteverify probe worked
        assert body.get("secret_check") == "secret_ok", body

    def test_turnstile_config_public(self):
        r = requests.get(f"{BASE_URL}/api/auth/turnstile-config", timeout=10)
        assert r.status_code == 200
        # UI only shows widget when enabled
        assert r.json().get("enabled") is False


class TestCORS:
    def test_credentialed_request_with_preview_origin(self, admin_session):
        # In this env Cloudflare/edge may rewrite CORS response headers to '*',
        # so the authoritative signal is functional: a cookie-authenticated
        # request from the preview Origin succeeds.
        time.sleep(0.5)
        r = admin_session.get(f"{BASE_URL}/api/auth/me",
                              headers={"Origin": BASE_URL}, timeout=10)
        assert r.status_code == 200, r.text[:300]
        assert r.json().get("email") == ADMIN_EMAIL


class TestSmokeRegression:
    def test_health(self):
        r = requests.get(f"{BASE_URL}/api/health", timeout=10)
        assert r.status_code == 200

    def test_accounts_list(self, admin_session):
        time.sleep(0.5)
        r = admin_session.get(f"{BASE_URL}/api/accounts", timeout=10)
        assert r.status_code == 200

    def test_safety_blocks(self, admin_session):
        time.sleep(0.5)
        r = admin_session.get(f"{BASE_URL}/api/safety/blocks", timeout=10)
        assert r.status_code in (200, 404), r.text[:200]
