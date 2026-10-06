"""Main99 review backend integration tests (iter 237).

Scope:
- Admin login (cookie + CSRF).
- GET /api/admin/acceptance/current — contract + signature-leak guard.
- POST /api/admin/acceptance/bundle — graceful 4xx when no approved inventory expectation,
  or signed Ed25519 bundle with schema_version=2 + key_id.
- GET /api/authority/inventory + /api/authority/inventory/pending (admin vs non-admin).
- GET /api/crypto/exchanges — capability matrix per exchange; 401 unauthenticated.
- POST /api/accounts/{id}/trust-terminal — SEC-001 step-up MFA refusal (static code).
- GET /api/performance/verified — provenance.source_kind + share_allowed logic.
- Regression smoke: /api/status, /api/accounts, /api/bot/pulse, /api/admin/audit/verify.

Credentials are read from TEST_ADMIN_EMAIL / TEST_ADMIN_PASSWORD env vars — NEVER written to disk.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
import os
import re
import pytest
import requests

from live_target import require_live_base_url, resolve_admin_credentials  # noqa: E402

pytestmark = pytest.mark.integration

BASE_URL = require_live_base_url()          # skips the module when there is no live target (CI mongo-only lane)
ADMIN_EMAIL, ADMIN_PASSWORD = resolve_admin_credentials()


# ────────── helpers / fixtures ──────────
def _login(session: requests.Session, email: str, password: str) -> requests.Response:
    r = session.post(f"{BASE_URL}/api/auth/login",
                     json={"email": email, "password": password},
                     timeout=15)
    return r


def _csrf_headers(session: requests.Session) -> dict:
    tok = session.cookies.get("csrf_token")
    return {"X-CSRF-Token": tok} if tok else {}


@pytest.fixture(scope="module")
def admin_session() -> requests.Session:
    s = requests.Session()
    r = _login(s, ADMIN_EMAIL, ADMIN_PASSWORD)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    me = s.get(f"{BASE_URL}/api/auth/me", timeout=10)
    assert me.status_code == 200, me.text
    assert me.json().get("role") == "admin", me.json()
    return s


@pytest.fixture(scope="module")
def anon_session() -> requests.Session:
    return requests.Session()


# ────────── auth ──────────
class TestAuth:
    def test_login_sets_cookies(self):
        s = requests.Session()
        r = _login(s, ADMIN_EMAIL, ADMIN_PASSWORD)
        assert r.status_code == 200, r.text[:400]
        # httpOnly session + csrf cookie expected
        names = {c.name for c in s.cookies}
        assert "csrf_token" in names, f"csrf_token missing; cookies={names}"

    def test_me(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/auth/me", timeout=10)
        assert r.status_code == 200
        j = r.json()
        assert j.get("email") == ADMIN_EMAIL
        assert j.get("role") == "admin"


# ────────── acceptance bundle ──────────
class TestAcceptance:
    def test_current(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/acceptance/current", timeout=15)
        assert r.status_code == 200, r.text[:400]
        j = r.json()
        for k in ("required", "release", "latest", "valid_for_release", "reason", "accounts"):
            assert k in j, f"missing key {k} in {list(j.keys())}"
        latest = j.get("latest") or {}
        # SEC: signature must never leak out
        assert "signature" not in latest, "signature leaked in /acceptance/current.latest"

    def test_generate_bundle(self, admin_session):
        headers = _csrf_headers(admin_session)
        r = admin_session.post(f"{BASE_URL}/api/admin/acceptance/bundle",
                               headers=headers, json={}, timeout=30)
        # Expected scenarios: step-up required (403), unavailable (503/4xx), or success (200)
        assert r.status_code != 500, f"500 from acceptance/bundle: {r.text[:400]}"
        if r.status_code == 200:
            doc = r.json()
            assert doc.get("algo") == "ed25519", f"algo={doc.get('algo')}"
            assert doc.get("schema_version") == 2, f"schema_version={doc.get('schema_version')}"
            assert doc.get("key_id"), f"key_id missing: {list(doc.keys())}"
            payload = doc.get("payload") or {}
            inv = payload.get("inventory") or {}
            assert "approved_by" in inv, f"payload.inventory.approved_by missing: {list(inv.keys())}"
            # must NOT include signature in response
            assert "signature" not in doc, "signature leaked in bundle response"
            # Listing endpoint shows it
            lst = admin_session.get(f"{BASE_URL}/api/admin/acceptance/bundles", timeout=15)
            assert lst.status_code == 200
            bundles = lst.json().get("bundles") or []
            ids = [b.get("bundle_id") for b in bundles]
            assert doc.get("bundle_id") in ids
        else:
            # Must be a clean 4xx/5xx-but-not-500 with structured error
            assert r.status_code in (400, 403, 409, 503), f"unexpected status {r.status_code}: {r.text[:300]}"
            # detail must be structured code, not raw str(e)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            detail = body.get("detail", body)
            if isinstance(detail, dict):
                assert "code" in detail, f"no error code in detail: {detail}"
            # ensure no python traceback / Exception() repr leak
            assert "Traceback" not in r.text and "<class '" not in r.text


# ────────── inventory ──────────
class TestInventory:
    def test_inventory_admin(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/authority/inventory", timeout=15)
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        assert isinstance(j, dict)

    def test_inventory_pending_admin(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/authority/inventory/pending", timeout=15)
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        for k in ("expectation", "admin_count"):
            assert k in j

    def test_inventory_pending_requires_admin(self, anon_session):
        r = anon_session.get(f"{BASE_URL}/api/authority/inventory/pending", timeout=15)
        assert r.status_code in (401, 403), f"expected 401/403 got {r.status_code}"


# ────────── crypto exchanges ──────────
class TestCryptoExchanges:
    def test_requires_auth(self, anon_session):
        r = anon_session.get(f"{BASE_URL}/api/crypto/exchanges", timeout=15)
        assert r.status_code == 401, f"expected 401 got {r.status_code}: {r.text[:200]}"

    def test_capability_matrix(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/crypto/exchanges", timeout=20)
        assert r.status_code == 200, r.text[:400]
        j = r.json()
        exchanges = j.get("exchanges") or []
        assert len(exchanges) >= 1
        ids = {e["id"] for e in exchanges}
        # at least one well-known exchange present
        assert ids & {"binance", "kraken", "bybit", "coinbase"}, f"no known exchange in {ids}"
        for e in exchanges:
            assert "capabilities" in e, f"capabilities missing on {e.get('id')}"
            # cap matrix should be an object (or None if truly unmapped) — must not be missing
            caps = e["capabilities"]
            if caps is not None:
                assert isinstance(caps, dict), f"capabilities should be dict for {e['id']}: {caps}"


# ────────── trust-terminal / SEC-001 ──────────
class TestTrustTerminalStepUp:
    def test_requires_step_up_or_mfa_enrollment(self, admin_session):
        # Discover an MT5 account to attempt against (admin's own accounts)
        accs = admin_session.get(f"{BASE_URL}/api/accounts", timeout=15)
        assert accs.status_code == 200, accs.text[:300]
        rows = accs.json() if isinstance(accs.json(), list) else (accs.json() or {}).get("accounts", [])
        candidate = None
        for a in rows or []:
            if a.get("mode") != "paper" and not a.get("verified_identity"):
                candidate = a
                break
        if not candidate:
            pytest.skip("no non-paper, non-verified account available to probe trust-terminal step-up")
        aid = candidate.get("id") or candidate.get("_id")
        headers = _csrf_headers(admin_session)
        r = admin_session.post(f"{BASE_URL}/api/accounts/{aid}/trust-terminal",
                               headers=headers, json={}, timeout=15)
        # SEC-001: must be refused without step-up — never 500, never raw exception
        assert r.status_code != 500, f"500 from trust-terminal: {r.text[:400]}"
        assert r.status_code in (401, 403, 409), f"unexpected {r.status_code}: {r.text[:300]}"
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        detail = body.get("detail", body)
        # SEC-002: must be a structured code, not raw str(e)
        if isinstance(detail, dict):
            code = detail.get("code") or ""
            assert code, f"no code in detail: {detail}"
            # Expected codes for step-up / mfa flow
            # (step_up emits 'mfa_enrollment_required' or 'step_up_required')
            assert re.search(r"(step[_-]?up|mfa)", code, re.I), f"unexpected code '{code}'"
        else:
            # as a string, must at least mention step-up or mfa — not a traceback
            assert "Traceback" not in r.text
            assert re.search(r"(step[_-]?up|mfa|two[-_ ]?factor)", str(detail), re.I), \
                f"detail does not mention step-up/mfa: {detail!r}"


# ────────── verified performance / provenance ──────────
class TestPerformanceProvenance:
    def test_verified_shape(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/performance/verified", timeout=20)
        assert r.status_code == 200, r.text[:400]
        j = r.json()
        prov = j.get("provenance") or {}
        assert prov, f"provenance missing: {list(j.keys())}"
        sk = prov.get("source_kind")
        assert sk in ("derived", "broker_reconciled"), f"source_kind={sk!r}"
        assert "ledger_gate_reasons" in prov and isinstance(prov["ledger_gate_reasons"], list)
        # share_allowed must be False when derived
        if sk == "derived":
            assert prov.get("share_allowed") is False, f"share_allowed must be False when derived: {prov.get('share_allowed')}"
            assert j.get("share_allowed") is False


# ────────── regression smoke ──────────
class TestRegressionSmoke:
    @pytest.mark.parametrize("path", [
        "/api/status",
        "/api/accounts",
        "/api/bot/pulse",
        "/api/admin/audit/verify",
    ])
    def test_endpoint_ok(self, admin_session, path):
        r = admin_session.get(f"{BASE_URL}{path}", timeout=20)
        assert r.status_code != 500, f"500 from {path}: {r.text[:300]}"
        assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:300]}"
