from live_target import ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-150 SEC-001 fix verification — cross-tenant revoke_public now
enforces require_admin (mirrors issue/view). Preview has
ADMIN_MFA_ENFORCED=false so admin flow still works end-to-end via HTTP;
enforcement is verified at unit-level with a monkey-patched env.
"""
import os
import re
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/") or "http://localhost:8001"
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@stoicaibot.com"
ADMIN_PASS = ADMIN_PASSWORD
NON_ADMIN_EMAIL = "ccnon_11613bd0@example.com"
NON_ADMIN_PASS = "Kd5#Zt9mW2xVpR7c"


def _login(session, email, password):
    r = session.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=15)
    assert r.status_code == 200, f"login failed {email}: {r.status_code} {r.text[:200]}"
    csrf = session.cookies.get("csrf_token")
    assert csrf, "csrf_token cookie missing after login"
    session.headers.update({"X-CSRF-Token": csrf})
    return r.json()


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    _login(s, ADMIN_EMAIL, ADMIN_PASS)
    return s


@pytest.fixture(scope="module")
def nonadmin_session():
    s = requests.Session()
    _login(s, NON_ADMIN_EMAIL, NON_ADMIN_PASS)
    return s


@pytest.fixture(scope="module")
def admin_account_id(admin_session):
    """Find an admin-owned account. Create one if none exists."""
    r = admin_session.get(f"{API}/accounts", timeout=15)
    assert r.status_code == 200, r.text[:200]
    accts = r.json() if isinstance(r.json(), list) else r.json().get("accounts", [])
    if accts:
        return accts[0].get("id") or accts[0].get("_id")
    # create one
    payload = {"broker": "IC Markets", "account_number": "9999150", "label": "iter150-test",
               "server": "ICMarketsSC-Demo01", "platform": "MT5", "account_type": "demo"}
    r = admin_session.post(f"{API}/accounts", json=payload, timeout=15)
    assert r.status_code in (200, 201), r.text[:200]
    return r.json().get("id") or r.json().get("_id")


# ─────────── FIX VERIFICATION: admin revoke still works in preview ────────────

class TestAdminRevokeE2E:
    def test_center_returns_six_pillars(self, admin_session, admin_account_id):
        r = admin_session.get(f"{API}/certification/center", params={"account_id": admin_account_id}, timeout=20)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert data.get("pillars_total") == 6
        assert len(data.get("pillars") or []) == 6

    def test_issue_then_revoke_by_admin(self, admin_session, admin_account_id):
        # issue
        r = admin_session.post(f"{API}/certification/center/issue",
                               json={"account_id": admin_account_id}, timeout=25)
        # if issuance cap already hit for the day, take an existing non-revoked cert
        cert_id = None
        if r.status_code == 200:
            cert_id = r.json().get("cert_id")
        elif r.status_code == 429:
            gr = admin_session.get(f"{API}/certification/center",
                                   params={"account_id": admin_account_id}, timeout=20)
            for c in gr.json().get("certificates") or []:
                if not c.get("revoked"):
                    cert_id = c.get("cert_id")
                    break
            assert cert_id, "no re-usable non-revoked cert & issuance cap hit"
            # confirm cap message format
            assert "cap" in (r.json().get("detail") or "").lower()
        else:
            pytest.fail(f"issue failed: {r.status_code} {r.text[:200]}")

        assert cert_id and re.match(r"^STC-[0-9A-F]{10}$", cert_id), f"bad cert_id: {cert_id}"

        # revoke as admin — this is the SEC-001 code path
        r = admin_session.post(f"{API}/certification/center/revoke",
                               json={"cert_id": cert_id, "reason": "iter150-sec001-verify"}, timeout=15)
        assert r.status_code == 200, f"admin revoke failed: {r.status_code} {r.text[:300]}"
        body = r.json()
        assert body.get("ok") is True
        assert body.get("cert_id") == cert_id

        # public view reflects revocation
        p = requests.get(f"{API}/public/certificate/{cert_id}", timeout=15)
        assert p.status_code == 200, p.text[:200]
        pv = p.json()
        assert pv.get("revoked") is True
        assert pv.get("valid") is False
        # hash chain still verifies (revocation not part of issued hash)
        assert pv.get("hash_verified") is True


# ─────────── Non-admin CANNOT revoke another user's cert ───────────

class TestNonAdminCannotRevokeOthers:
    def test_nonadmin_revoke_admin_cert_returns_404(self, admin_session, admin_account_id,
                                                     nonadmin_session):
        # find or create a non-revoked admin-owned cert
        gr = admin_session.get(f"{API}/certification/center",
                               params={"account_id": admin_account_id}, timeout=20)
        cert_id = None
        for c in gr.json().get("certificates") or []:
            if not c.get("revoked"):
                cert_id = c.get("cert_id")
                break
        if not cert_id:
            ir = admin_session.post(f"{API}/certification/center/issue",
                                    json={"account_id": admin_account_id}, timeout=25)
            if ir.status_code == 200:
                cert_id = ir.json().get("cert_id")
        if not cert_id:
            pytest.skip("no live admin cert available for cross-tenant negative test")

        r = nonadmin_session.post(f"{API}/certification/center/revoke",
                                  json={"cert_id": cert_id, "reason": "malicious"}, timeout=15)
        # scoped query (user_id filter) => not found for non-owner
        assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text[:200]}"

        # cert unchanged
        p = requests.get(f"{API}/public/certificate/{cert_id}", timeout=15)
        assert p.status_code == 200
        assert p.json().get("revoked") is False
        assert p.json().get("valid") is True


# ─────────── Unit-level enforcement check (ADMIN_MFA_ENFORCED=true) ───────────

def test_revoke_public_admin_branch_calls_require_admin(monkeypatch):
    """When ADMIN_MFA_ENFORCED=true and admin lacks TOTP, revoke_public must
    raise admin_mfa_required — proves the require_admin gate is actually
    wired into the admin branch (SEC-001 fix)."""
    import asyncio
    from fastapi import HTTPException
    import certification_center as cc

    monkeypatch.setenv("ADMIN_MFA_ENFORCED", "true")

    class _FakeCollection:
        async def find_one_and_update(self, *a, **kw):
            # If the gate did NOT raise, we'd reach here and silently succeed.
            # Return a truthy doc so the caller returns ok:true — the test
            # will assert this DID NOT happen.
            return {"cert_id": "STC-DEADBEEF01"}

    class _FakeDB:
        public_certificates = _FakeCollection()

    admin_no_totp = {"id": "admin-uid-x", "role": "admin", "two_factor_enabled": False}

    async def _run():
        return await cc.revoke_public(_FakeDB(), "STC-DEADBEEF01", admin_no_totp, "unit-check")

    with pytest.raises(HTTPException) as ei:
        asyncio.run(_run())
    detail = ei.value.detail
    # detail may be dict {code:"admin_mfa_required", ...} or string
    if isinstance(detail, dict):
        assert detail.get("code") == "admin_mfa_required"
    else:
        assert "admin" in str(detail).lower()
    assert ei.value.status_code == 403


def test_revoke_public_user_branch_scoped_to_owner(monkeypatch):
    """With ADMIN_MFA_ENFORCED=true, a regular user revoking their OWN cert
    must NOT hit the admin gate — scoped by user_id filter."""
    import asyncio
    import certification_center as cc

    monkeypatch.setenv("ADMIN_MFA_ENFORCED", "true")

    captured = {}

    class _FakeCollection:
        async def find_one_and_update(self, q, upd):
            captured["q"] = q
            return {"cert_id": "STC-USEROWNED1"}

    class _FakeDB:
        public_certificates = _FakeCollection()

    regular_user = {"id": "user-uid-x", "role": "user", "two_factor_enabled": False}

    async def _run():
        return await cc.revoke_public(_FakeDB(), "STC-USEROWNED1", regular_user, "self")

    out = asyncio.run(_run())
    assert out == {"ok": True, "cert_id": "STC-USEROWNED1"}
    assert captured["q"].get("user_id") == "user-uid-x"
    assert captured["q"].get("revoked") is False


pytestmark = pytest.mark.http
