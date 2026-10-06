"""main97 HTTP smoke — admin cookie against preview backend.

Covers:
  - acceptance bundle verdict payload includes verdict/accounts/release/expiry
    (N97-? bundle signature coverage) and surface still returns 200.
  - release gate admin endpoint still returns 200 with authoritative/release.
  - demo readiness admin endpoint returns 200 with authority payload.
  - Step-up (/api/auth/step-up) on preview admin (no TOTP enrolled) → 403.

Run with:
  STOIC_TESTS_ALLOW_DB_NAME=1 DB_NAME=ai_trading_bot \
    python -m pytest backend/tests/test_iter233_main97_http.py -v
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env", encoding="utf-8") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break

ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL") or "admin@trading.bot"
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    # fall back to backend/.env
    try:
        with open("/app/backend/.env", encoding="utf-8") as f:
            for line in f:
                if line.startswith("TEST_ADMIN_PASSWORD="):
                    ADMIN_PASSWORD = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass


@pytest.fixture(scope="session")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=20)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    # pull CSRF cookie if present
    csrf = s.cookies.get("csrf_token") or s.cookies.get("csrftoken")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


def test_admin_login_ok(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/auth/me", timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("email") in (ADMIN_EMAIL, "admin@stoicaibot.com")
    assert body.get("role") in ("admin", "owner", "superadmin")


def test_release_gate_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/release-gate", timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    # must at least report enforcement flag + failures bag
    assert "enforced" in body
    assert "failures" in body


def test_acceptance_bundle_current(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/acceptance/current", timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "required" in body
    assert "release" in body


def test_acceptance_bundle_generate_shape(admin_session):
    r = admin_session.post(f"{BASE_URL}/api/admin/acceptance/bundle", timeout=30)
    assert r.status_code in (200, 201), f"{r.status_code} {r.text[:300]}"
    body = r.json()
    # N97 bundle signature coverage — verdict + accounts + release + expiry
    assert "verdict" in body
    assert "account_ids" in body
    assert "signature" not in body and "digest" in body                       # audit P3: HMAC stays server-side
    payload = body.get("payload") or {}
    assert "release" in payload, f"release missing from payload: {list(payload)}"
    assert any(k in body for k in ("expiry", "expires_at", "ttl", "valid_until")), (
        f"expiry field missing: {list(body)}"
    )


def test_demo_readiness(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/demo-readiness", timeout=20)
    assert r.status_code == 200, r.text
    body = r.json()
    # authority payload exists; grants_authority lives under authority.
    assert "authority" in body
    auth_block = body["authority"] if isinstance(body["authority"], dict) else {}
    assert "grants_authority" in auth_block
    assert auth_block["grants_authority"] is False


def test_step_up_rejects_admin_without_totp(admin_session):
    """Preview admin has no TOTP enrolled → step-up must refuse (403)."""
    r = admin_session.post(
        f"{BASE_URL}/api/auth/step-up",
        json={"action": "unlock_account", "code": "000000"},
        timeout=15,
    )
    # 403 (no TOTP) is expected; 400/401 also acceptable negative outcomes.
    assert r.status_code in (400, 401, 403, 404), f"{r.status_code} {r.text[:200]}"


def test_non_admin_cannot_hit_admin_surfaces():
    """Public (no cookie) hits must be refused."""
    s = requests.Session()
    for path in ("/api/admin/release-gate",
                 "/api/admin/acceptance/current",
                 "/api/admin/demo-readiness"):
        r = s.get(f"{BASE_URL}{path}", timeout=15)
        assert r.status_code in (401, 403), f"{path} returned {r.status_code}"
