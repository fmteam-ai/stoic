"""
Iter-216 / Round-20 security audit verification (STRICTLY READ-ONLY on preview).

Covers:
  - GET /api/health → 200, ea_version == '1.57'
  - GET /api/status → 200, trading.readiness.dominant_code present, 6 components
  - GET /api/authority/decision (admin) → 200 with state, dominant_code, blockers;
      any live-mode trading_enabled account must carry an EA blocker among
      EA_CAPABILITY_BELOW_MIN / EA_RELEASE_HASH_UNPINNED / EA_BINARY_PROOF_MISSING /
      EA_BINARY_HASH_MISMATCH / EA_VERSION_UNKNOWN, and new_exposure_allowed==False.
  - GET /api/accounts (admin) → 200, report ea_version / trading_enabled.
  - GET /api/nl/triggers → 200
  - GET /api/ops/deploy-preflight (admin) → 200; body includes mongo_transactions signal.
  - GET /api/ops/deploy-watch → 200 (observe only; do NOT cancel).

No mutating calls anywhere in this module.
"""
import os
import sys
import json
import requests
import pytest

sys.path.insert(0, "/app/backend")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_target import require_live_base_url  # noqa: E402

pytestmark = pytest.mark.http

BASE = require_live_base_url().rstrip("/")
if not (os.environ.get("TEST_ADMIN_EMAIL") and os.environ.get("TEST_ADMIN_PASSWORD")):
    pytest.skip("TEST_ADMIN_EMAIL / TEST_ADMIN_PASSWORD not set", allow_module_level=True)

ADMIN_EMAIL = os.environ["TEST_ADMIN_EMAIL"]
ADMIN_PW = os.environ["TEST_ADMIN_PASSWORD"]

EA_BLOCKER_CODES = {
    "EA_CAPABILITY_BELOW_MIN",
    "EA_VERSION_UNKNOWN",
    "EA_RELEASE_HASH_UNPINNED",
    "EA_BINARY_PROOF_MISSING",
    "EA_BINARY_HASH_MISMATCH",
}


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    csrf = s.cookies.get("csrf_token")
    assert csrf, "csrf_token cookie missing"
    s.headers.update({"X-CSRF-Token": csrf})
    return s


# ---------- public ---------- #
def test_health_ea_157():
    r = requests.get(f"{BASE}/api/health", timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert j.get("status") in ("ok", "healthy"), j
    assert j.get("ea_version") == "1.57", j


def test_status_dominant_code_and_components():
    r = requests.get(f"{BASE}/api/status", timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert j.get("overall") in ("operational", "degraded", "major_outage"), j
    tr = j.get("trading") or {}
    readiness = tr.get("readiness") or {}
    assert isinstance(readiness, dict) and readiness.get("dominant_code"), tr
    comps = j.get("components") or {}
    for c in ("api", "database", "bot_engine", "ea_bridge", "payments", "email"):
        assert c in comps, f"component missing: {c}"


# ---------- authority decision + EA blocker cross-check ---------- #
def test_authority_ea_blocker_and_no_new_exposure(admin_session):
    r = admin_session.get(f"{BASE}/api/authority/decision", timeout=20)
    assert r.status_code == 200, r.text[:200]
    j = r.json()
    assert "state" in j and "dominant_code" in j, j
    blockers = j.get("blockers") or []
    codes = [(b.get("code") if isinstance(b, dict) else b) for b in blockers]
    print(f"  authority.state={j.get('state')} dominant_code={j.get('dominant_code')}")
    print(f"  authority.blockers={codes}")
    print(f"  new_exposure_allowed={j.get('new_exposure_allowed')}")

    # Cross-check with accounts
    r2 = admin_session.get(f"{BASE}/api/accounts", timeout=20)
    assert r2.status_code == 200
    body = r2.json()
    items = body if isinstance(body, list) else (body.get("accounts") or body.get("items") or [])
    live_trading = [a for a in items
                    if (a.get("mode") or a.get("account_mode") or a.get("type")) == "live"
                    and bool(a.get("trading_enabled"))]
    for a in items:
        print(f"  account id={a.get('id') or a.get('account_id')} "
              f"mode={a.get('mode') or a.get('account_mode') or a.get('type')} "
              f"trading_enabled={a.get('trading_enabled')} "
              f"ea_version={a.get('ea_version') or a.get('eaVersion')}")

    if live_trading:
        assert any(c in EA_BLOCKER_CODES for c in codes), (
            f"live trading account present but no EA blocker in {codes}. "
            f"Expected one of {EA_BLOCKER_CODES}"
        )
        # No new exposure is allowed while EA gate is unresolved
        assert j.get("new_exposure_allowed") is False, j


# ---------- read-only regressions ---------- #
def test_nl_triggers_200(admin_session):
    r = admin_session.get(f"{BASE}/api/nl/triggers", timeout=15)
    assert r.status_code == 200, r.text[:200]


def test_deploy_preflight_mongo_transactions(admin_session):
    r = admin_session.get(f"{BASE}/api/ops/deploy-preflight", timeout=20)
    assert r.status_code == 200, r.text[:200]
    body_txt = r.text
    # The Round-20 P2-05 change requires a mongo_transactions signal in preflight
    assert "mongo_transactions" in body_txt, f"mongo_transactions missing in preflight: {body_txt[:400]}"
    try:
        j = r.json()
        print("  preflight keys:", list(j.keys())[:20])
    except Exception:
        pass


def test_deploy_watch_readable(admin_session):
    r = admin_session.get(f"{BASE}/api/ops/deploy-watch", timeout=15)
    assert r.status_code == 200, r.text[:200]
    j = r.json()
    watch = j.get("watch") if isinstance(j.get("watch"), dict) else j
    status = (watch or {}).get("status")
    print(f"  deploy_watch.status={status}")
    # Observation only — do NOT cancel or mutate.
    assert status in ("watching", "armed", "live", "completed", "cancelled", "incident", None), watch
