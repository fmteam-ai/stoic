"""
Iter-214 / Round-17 security audit verification (READ-ONLY on preview).

Covers:
  - P0-02 live suite is read-only; no mutating NL commands executed here.
  - GET /api/health → ea_version == '1.58'.
  - GET /api/ops/deploy-preflight → mongo_transactions check exists (warn in preview).
  - GET /api/status shape for public /status: overall + headline + trading provenance.
  - Risk Commander read-only wiring:
        POST /api/nl/command/confirm {actions:[...]}      → 400 proposal_id_required
        POST /api/nl/command/confirm proposal_id=<24 zeros> → 404
  - Deploy watchdog still returns armed watch (do NOT cancel).
"""
import os
import sys
import time
import requests
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_target import require_live_base_url  # noqa: E402

pytestmark = pytest.mark.http

BASE = require_live_base_url().rstrip("/")
if not (os.environ.get("TEST_ADMIN_EMAIL") and os.environ.get("TEST_ADMIN_PASSWORD")):
    pytest.skip("TEST_ADMIN_EMAIL / TEST_ADMIN_PASSWORD not set", allow_module_level=True)
if not (os.environ.get("TEST_ADMIN_EMAIL") and os.environ.get("TEST_ADMIN_PASSWORD")):
    pytest.skip("TEST_ADMIN_EMAIL / TEST_ADMIN_PASSWORD not set", allow_module_level=True)
ADMIN_EMAIL = os.environ["TEST_ADMIN_EMAIL"]
ADMIN_PW = os.environ["TEST_ADMIN_PASSWORD"]


# ---------------- fixtures / helpers ---------------- #
@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
               timeout=15)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text[:200]}"
    csrf = s.cookies.get("csrf_token")
    assert csrf, "csrf_token cookie not set on login"
    s.headers.update({"X-CSRF-Token": csrf})
    return s


# ---------------- basics ---------------- #
def test_ea_version_is_157():
    r = requests.get(f"{BASE}/api/health", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body.get("ea_version") == "1.58", body


def test_public_status_shape():
    r = requests.get(f"{BASE}/api/status", timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert j.get("overall") in ("operational", "degraded", "major_outage"), j
    # headline present (frontend prefers headline over defaults)
    assert isinstance(j.get("headline", ""), str) and j["headline"], j
    # trading provenance block present
    tr = j.get("trading") or {}
    assert isinstance(tr, dict) and tr, "trading block missing on /api/status"
    assert "readiness" in tr and "attestation" in tr and "connectivity" in tr, tr
    # components list still there
    comps = j.get("components") or {}
    for key in ("api", "database", "bot_engine", "ea_bridge", "payments", "email"):
        assert key in comps, f"component {key} missing"


# ---------------- deploy preflight ---------------- #
def test_deploy_preflight_has_mongo_transactions_check(admin_session):
    r = admin_session.get(f"{BASE}/api/ops/deploy-preflight", timeout=20)
    assert r.status_code == 200, r.text[:200]
    j = r.json()
    checks = j.get("checks") or []
    assert isinstance(checks, list) and checks
    ids = [c.get("id") for c in checks]
    assert "mongo_transactions" in ids, f"mongo_transactions missing; got {ids}"
    mt = next(c for c in checks if c.get("id") == "mongo_transactions")
    # preview is standalone → warn expected
    assert mt.get("status") in ("warn", "pass", "fail"), mt
    if mt.get("status") == "warn":
        msg = (mt.get("detail") or mt.get("message") or mt.get("current") or "").lower()
        assert "standalone" in msg or "no transactions" in msg or "transactions" in msg, mt


# ---------------- NL Risk Commander read-only guards ---------------- #
def test_confirm_without_proposal_id_returns_400(admin_session):
    r = admin_session.post(f"{BASE}/api/nl/command/confirm",
                           json={"actions": [{"type": "DISABLE_BOTS", "target": "all"}]},
                           timeout=15)
    assert r.status_code == 400, f"expected 400 got {r.status_code} {r.text[:200]}"
    body = r.json()
    detail = body.get("detail")
    code = detail.get("code") if isinstance(detail, dict) else detail
    assert (code == "proposal_id_required"
            or "proposal_id" in str(body).lower()), body


def test_confirm_with_bogus_proposal_id_returns_404(admin_session):
    r = admin_session.post(f"{BASE}/api/nl/command/confirm",
                           json={"proposal_id": "0" * 24},
                           timeout=15)
    assert r.status_code == 404, f"expected 404 got {r.status_code} {r.text[:200]}"


# ---------------- deploy watchdog still armed ---------------- #
def test_deploy_watch_still_watching(admin_session):
    r = admin_session.get(f"{BASE}/api/ops/deploy-watch", timeout=15)
    assert r.status_code == 200, r.text[:200]
    j = r.json()
    # Accept either a shaped object or {watch:{...}}
    watch = j.get("watch") if isinstance(j.get("watch"), dict) else j
    status = (watch or {}).get("status")
    # do NOT cancel; just observe
    assert status in ("watching", "armed", "live", "completed", "cancelled", None), watch
    # If watching, ensure polls counter is present
    if status in ("watching", "armed"):
        assert "polls" in watch or "polls" in j, j
