"""
Iter-215 / Round-18 security audit verification (READ-ONLY on preview).

Covers:
  - GET /api/health → status ok, ea_version == '1.59'
  - GET /api/status → overall + trading.readiness (dominant_code present) + 6 components
  - GET /api/authority/decision (admin) → 200 with state, dominant_code, blockers list
  - GET /api/accounts (admin) → for each account report ea_version + trading_enabled;
      any live-mode trading_enabled account with ea_version < 1.57 must produce an EA
      blocker (EA_CAPABILITY_BELOW_MIN or EA_VERSION_UNKNOWN) in the authority decision.
  - GET /api/nl/triggers → 200
  - GET /api/ops/deploy-watch → 200 (observe status; do NOT cancel).

Strictly read-only: no mutating NL commands, no PANIC, no activation flips, no
deploy watch cancel.
"""
import os
import sys
import requests
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_target import require_live_base_url  # noqa: E402

pytestmark = pytest.mark.http

BASE = require_live_base_url().rstrip("/")
if not (os.environ.get("TEST_ADMIN_EMAIL") and os.environ.get("TEST_ADMIN_PASSWORD")):
    pytest.skip("TEST_ADMIN_EMAIL / TEST_ADMIN_PASSWORD not set", allow_module_level=True)

ADMIN_EMAIL = os.environ["TEST_ADMIN_EMAIL"]
ADMIN_PW = os.environ["TEST_ADMIN_PASSWORD"]


def _ver_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return None


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
    assert j.get("ea_version") == "1.59", j


def test_status_shape_and_dominant_code():
    r = requests.get(f"{BASE}/api/status", timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert j.get("overall") in ("operational", "degraded", "major_outage"), j
    tr = j.get("trading") or {}
    readiness = tr.get("readiness") or {}
    # readiness may be dict with state/dominant_code
    assert isinstance(readiness, dict) and readiness, tr
    assert readiness.get("dominant_code"), readiness
    comps = j.get("components") or {}
    for c in ("api", "database", "bot_engine", "ea_bridge", "payments", "email"):
        assert c in comps, f"component missing: {c}"


# ---------- authority decision ---------- #
def test_authority_decision(admin_session):
    r = admin_session.get(f"{BASE}/api/authority/decision", timeout=20)
    assert r.status_code == 200, r.text[:200]
    j = r.json()
    assert "state" in j and "dominant_code" in j, j
    blockers = j.get("blockers") or []
    assert isinstance(blockers, list), j
    # capture codes for the cross-check
    admin_session._authority_codes = [
        (b.get("code") if isinstance(b, dict) else b) for b in blockers
    ]


# ---------- accounts ↔ authority EA blocker cross-check ---------- #
def test_accounts_ea_capability_blocker(admin_session):
    r = admin_session.get(f"{BASE}/api/accounts", timeout=20)
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    items = body if isinstance(body, list) else (body.get("accounts") or body.get("items") or [])
    # Report and cross-check
    live_below_min = False
    live_unknown = False
    for a in items:
        mode = a.get("mode") or a.get("account_mode") or a.get("type")
        te = bool(a.get("trading_enabled"))
        ver = a.get("ea_version") or a.get("eaVersion")
        print(f"  account id={a.get('id') or a.get('account_id')} mode={mode} "
              f"trading_enabled={te} ea_version={ver}")
        if mode == "live" and te:
            if ver is None or str(ver).lower() in ("", "unknown"):
                live_unknown = True
            else:
                vt = _ver_tuple(ver)
                min_vt = _ver_tuple("1.57")
                if vt is not None and min_vt is not None and vt < min_vt:
                    live_below_min = True

    # Fetch authority codes (fresh)
    r2 = admin_session.get(f"{BASE}/api/authority/decision", timeout=20)
    assert r2.status_code == 200
    codes = [
        (b.get("code") if isinstance(b, dict) else b)
        for b in (r2.json().get("blockers") or [])
    ]
    print(f"  authority blockers: {codes}")

    if live_below_min:
        assert "EA_CAPABILITY_BELOW_MIN" in codes, (
            f"live account below 1.57 present but EA_CAPABILITY_BELOW_MIN missing: {codes}"
        )
    if live_unknown:
        assert ("EA_VERSION_UNKNOWN" in codes
                or "EA_CAPABILITY_BELOW_MIN" in codes), (
            f"live account with unknown EA but no EA blocker: {codes}"
        )


# ---------- read-only regressions ---------- #
def test_nl_triggers_200(admin_session):
    r = admin_session.get(f"{BASE}/api/nl/triggers", timeout=15)
    assert r.status_code == 200, r.text[:200]


def test_deploy_watch_readable(admin_session):
    r = admin_session.get(f"{BASE}/api/ops/deploy-watch", timeout=15)
    assert r.status_code == 200, r.text[:200]
    j = r.json()
    watch = j.get("watch") if isinstance(j.get("watch"), dict) else j
    status = (watch or {}).get("status")
    print(f"  deploy_watch.status={status}")
    # Just observe; accept any known state, do NOT cancel.
    assert status in ("watching", "armed", "live", "completed", "cancelled", "incident", None), watch
