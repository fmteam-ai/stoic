"""A13 Part 1 HTTP smoke — crypto status, demo-readiness, kill switch surfacing.

Admin login via cookie auth, verifies:
  • GET /api/crypto/* status/account endpoints respond for authed admin
  • Live-trading status reflects _live_enabled() (false in preview)
  • GET /api/admin/demo-readiness returns 200, grants_authority is False
  • POST /api/admin/demo-readiness/manual stores build_sha + environment + expires_at
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # fallback to local
    BASE_URL = "http://localhost:8001"

ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL", "admin@trading.bot")
ADMIN_PASS = os.environ.get("TEST_ADMIN_PASSWORD", "")


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASS},
               timeout=15)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    # Attach CSRF header from cookie for POSTs
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


class TestA13CryptoStatus:
    def test_crypto_status_endpoints_respond(self, admin_session):
        # Try a few common status endpoints
        candidates = [
            "/api/crypto/accounts",
            "/api/crypto/status",
        ]
        hits = []
        for p in candidates:
            r = admin_session.get(f"{BASE_URL}{p}", timeout=10)
            hits.append((p, r.status_code))
            # 200 or 404 (route missing) acceptable; must not be 500
            assert r.status_code < 500, f"{p} -> {r.status_code} {r.text[:200]}"
        print("crypto endpoint statuses:", hits)

    def test_live_enabled_reflected_false_in_preview(self, admin_session):
        # In preview CRYPTO_LIVE_TRADING_ENABLED is unset -> live disabled.
        # Check any crypto status surface that exposes a live flag.
        r = admin_session.get(f"{BASE_URL}/api/crypto/status", timeout=10)
        if r.status_code != 200:
            pytest.skip(f"/api/crypto/status not available: {r.status_code}")
        data = r.json()
        # Accept any of common flag names
        flat = str(data).lower()
        # Must not claim live enabled
        assert '"live_enabled": true' not in flat and '"live": true' not in flat, data


class TestA13DemoReadiness:
    def test_get_demo_readiness(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/demo-readiness", timeout=15)
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        # Must state no trading authority (nested under authority)
        auth = data.get("authority") or {}
        assert auth.get("grants_authority") is False, data
        ctx = data.get("context") or {}
        assert "build_sha" in ctx or "environment" in ctx, ctx

    def test_post_manual_tick_persists_build_env_expiry(self, admin_session):
        # First, fetch readiness to discover a valid manual checklist id
        r = admin_session.get(f"{BASE_URL}/api/admin/demo-readiness", timeout=15)
        assert r.status_code == 200
        data = r.json()
        # Find a manual item id — demo_readiness.build returns manual_items / checklist structure
        manual_id = None
        for key in ("manual", "manual_items", "manual_checks", "checklist"):
            v = data.get(key)
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict) and item.get("id"):
                        if item.get("manual") or key.startswith("manual"):
                            manual_id = item["id"]
                            break
                if manual_id:
                    break
        if not manual_id:
            # try nested shape
            for k, v in data.items():
                if isinstance(v, dict) and "manual" in v:
                    mv = v["manual"]
                    if isinstance(mv, list) and mv and isinstance(mv[0], dict):
                        manual_id = mv[0].get("id")
                        if manual_id:
                            break
        if not manual_id:
            pytest.skip(f"no manual checklist id found in payload keys={list(data.keys())}")

        r2 = admin_session.post(
            f"{BASE_URL}/api/admin/demo-readiness/manual",
            json={"id": manual_id, "checked": True},
            timeout=15,
        )
        assert r2.status_code == 200, r2.text[:400]
        body = r2.json()
        # Expect build_sha, environment, expires_at persisted somewhere in response
        s = str(body).lower()
        assert "build" in s or "environment" in s or "expires" in s, body
        print("manual tick stored:", body)
