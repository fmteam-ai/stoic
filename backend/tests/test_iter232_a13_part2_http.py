"""A13 Part 2 HTTP smoke — admin acceptance bundle + release gate endpoints.

Verifies (admin cookie auth + X-CSRF-Token for POSTs):
  * GET /api/admin/release-gate         → 200, enforced flag + failures list
  * GET /api/admin/acceptance/current   → 200, release + required flag + reason
  * POST /api/admin/acceptance/bundle   → 200, verdict FAIL (stale fleet) + signature + payload.accounts
  * GET /api/admin/acceptance/bundles   → list (no payload)
  * Non-admin user → 403 on all four
"""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/") or "http://localhost:8001"
ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL", "admin@trading.bot")
ADMIN_PASS = os.environ.get("TEST_ADMIN_PASSWORD", "")


def _login(email, password):
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": email, "password": password}, timeout=15)
    if r.status_code != 200:
        return None, r
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s, r


@pytest.fixture(scope="module")
def admin_session():
    s, r = _login(ADMIN_EMAIL, ADMIN_PASS)
    if not s:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    return s


@pytest.fixture(scope="module")
def non_admin_session():
    """Register a fresh non-admin user, verify via direct DB, then login."""
    s = requests.Session()
    email = f"t2_{uuid.uuid4().hex[:10]}@example.com"
    pwd = "N0tTheAdm1n!Pass#2026"
    r = s.post(f"{BASE_URL}/api/auth/register",
               json={"email": email, "password": pwd, "full_name": "T2 User",
                     "terms_agreed": True},
               timeout=15)
    if r.status_code not in (200, 201):
        pytest.skip(f"non-admin register failed: {r.status_code} {r.text[:200]}")
    # Mark verified directly in DB (preview Mongo on localhost)
    try:
        from pymongo import MongoClient
        mc = MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
        db = mc[os.environ.get("DB_NAME", "ai_trading_bot")]
        db.users.update_one({"email": email}, {"$set": {"email_verified": True,
                                                        "is_active": True,
                                                        "status": "active"}})
    except Exception as e:
        pytest.skip(f"could not mark user verified: {e}")
    s2, r2 = _login(email, pwd)
    if not s2:
        pytest.skip(f"non-admin login failed: {r2.status_code} {r2.text[:200]}")
    return s2


class TestReleaseGate:
    def test_release_gate_admin(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/release-gate", timeout=15)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        # Preview = enforced:false
        assert "enforced" in data
        assert data["enforced"] is False
        assert "ok" in data
        assert "failures" in data and isinstance(data["failures"], list)
        assert "lock_commit" in data
        assert "image_digest" in data

    def test_release_gate_forbidden_for_non_admin(self, non_admin_session):
        r = non_admin_session.get(f"{BASE_URL}/api/admin/release-gate", timeout=15)
        assert r.status_code == 403, f"expected 403 got {r.status_code}: {r.text[:200]}"


class TestAcceptanceCurrent:
    def test_current_admin(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/acceptance/current", timeout=15)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert "required" in data
        assert data["required"] is False  # preview
        assert "release" in data
        assert "build_sha" in data["release"]
        assert "valid_for_release" in data
        assert "reason" in data

    def test_current_forbidden_for_non_admin(self, non_admin_session):
        r = non_admin_session.get(f"{BASE_URL}/api/admin/acceptance/current", timeout=15)
        assert r.status_code == 403


class TestAcceptanceBundleGenerate:
    def test_generate_bundle_admin(self, admin_session):
        r = admin_session.post(f"{BASE_URL}/api/admin/acceptance/bundle",
                               json={}, timeout=30)
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        # Preview fleet is stale → verdict FAIL
        assert data.get("verdict") == "FAIL", f"verdict={data.get('verdict')}"
        assert "failures" in data and isinstance(data["failures"], list)
        assert len(data["failures"]) > 0
        assert "digest" in data
        assert "signature" not in data and len(data.get("digest", "")) == 64      # audit P3: HMAC stays server-side
        payload = data.get("payload") or {}
        accounts = payload.get("accounts")
        assert isinstance(accounts, list)
        # Each account has nested blocks (if any accounts in preview)
        for acc in accounts:
            for key in ("ea_session", "reconciliation", "positions", "authority", "statement"):
                assert key in acc, f"account missing {key}: {list(acc.keys())}"

    def test_generate_bundle_forbidden_for_non_admin(self, non_admin_session):
        r = non_admin_session.post(f"{BASE_URL}/api/admin/acceptance/bundle",
                                   json={}, timeout=15)
        assert r.status_code == 403


class TestAcceptanceBundlesList:
    def test_list_admin(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/acceptance/bundles", timeout=15)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        # Could be a list directly or {bundles: [...]}
        items = data if isinstance(data, list) else data.get("bundles", data.get("items", []))
        assert isinstance(items, list)
        for b in items:
            assert "payload" not in b or b["payload"] in (None, {}), "list endpoint must omit full payload"

    def test_list_forbidden_for_non_admin(self, non_admin_session):
        r = non_admin_session.get(f"{BASE_URL}/api/admin/acceptance/bundles", timeout=15)
        assert r.status_code == 403
