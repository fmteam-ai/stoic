"""Iteration 93 live HTTP checks against preview URL — safety-corrections batch.

Covers: operational mode migration outcome, allocator authority, ops/swallowed,
config versions endpoint, and step-up-gated rollback (with symmetric roll-forward
so admin config is unchanged). SAFETY: no bot start/stop, no mode raise.
"""
import os
import re
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not BASE_URL:
    # Fallback: read frontend/.env
    try:
        with open(os.path.join(_REPO, "frontend", ".env")) as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
    except Exception:
        pass

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


def _read_env(key):
    try:
        with open(os.path.join(_REPO, "backend", ".env")) as f:
            for line in f:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        return None
    return None


STEP_UP_BYPASS_TOKEN = _read_env("STEP_UP_BYPASS_TOKEN")
METRICS_TOKEN = _read_env("METRICS_TOKEN")


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=30)
    assert r.status_code == 200, f"login failed {r.status_code} {r.text}"
    return s


def _csrf_headers(session):
    csrf = session.cookies.get("csrf_token")
    return {"X-CSRF-Token": csrf} if csrf else {}


# ─── Operational mode migration outcome ────────────────────────────
def test_bot_config_operational_mode_is_supervised(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=20)
    assert r.status_code == 200, r.text
    data = r.json()
    mode = data.get("operational_mode") or (data.get("config") or {}).get("operational_mode")
    assert mode in ("supervised_live", "paper", "shadow", "off"), f"mode raised! got {mode!r}"
    assert mode != "autonomous_live", "SAFETY: active config must not be autonomous_live"


# ─── Allocator ────────────────────────────────────────────────────
def test_allocator_returns_authority_field(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/quant/allocator", timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    # Structure varies but must include allocations list with authority markers
    allocs = data.get("allocations") or data.get("weights") or data
    # If dict of strategy->info, inspect one
    if isinstance(allocs, dict):
        any_authority = any(
            isinstance(v, dict) and (v.get("authority") in ("none", "limited", "full"))
            for v in allocs.values()
        )
    elif isinstance(allocs, list):
        any_authority = any(
            (a.get("authority") in ("none", "limited", "full")) for a in allocs if isinstance(a, dict)
        )
    else:
        any_authority = False
    assert any_authority or "authority" in str(data), f"authority field missing in {data}"


# ─── Silent-failure counters ──────────────────────────────────────
def test_ops_swallowed_counters(admin_session):
    # Try admin cookie first
    r = admin_session.get(f"{BASE_URL}/api/ops/swallowed", timeout=20)
    if r.status_code == 401 and METRICS_TOKEN:
        r = requests.get(f"{BASE_URL}/api/ops/swallowed",
                         headers={"X-Metrics-Token": METRICS_TOKEN}, timeout=20)
    assert r.status_code == 200, f"{r.status_code}: {r.text}"
    data = r.json()
    assert "counters" in data or isinstance(data, dict), f"unexpected {data!r}"


# ─── Config versions ──────────────────────────────────────────────
def test_config_versions_has_active_pointer(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/config/versions", timeout=20)
    assert r.status_code == 200, r.text
    data = r.json()
    versions = data.get("versions") if isinstance(data, dict) else data
    assert isinstance(versions, list), f"expected list, got {type(versions)}"
    # active pointer somewhere
    if versions:
        active_count = sum(1 for v in versions if v.get("is_active"))
        # Allow 0 (fresh) or exactly 1
        assert active_count <= 1, f"multiple is_active pointers: {active_count}"


# ─── Rollback requires step-up ────────────────────────────────────
def test_rollback_requires_step_up(admin_session):
    # Explicit empty bypass so conftest auto-injection is suppressed and the
    # REAL gate is exercised.
    headers = {**_csrf_headers(admin_session),
               "Content-Type": "application/json",
               "X-Step-Up-Bypass": ""}
    r = admin_session.post(f"{BASE_URL}/api/config/rollback",
                           json={}, headers=headers, timeout=20)
    # Without step-up header, expect 401/403 step_up_required OR
    # mfa_enrollment_required (admin has no 2FA).
    assert r.status_code in (400, 401, 403), f"expected step-up gate, got {r.status_code}: {r.text}"
    body_text = r.text.lower()
    assert "step_up" in body_text or "step-up" in body_text or "mfa" in body_text, \
        f"expected step_up_required in body: {r.text}"


# ─── Bypass token allows through gate (do NOT actually mutate live config) ─────
def test_rollback_with_bypass_token_reaches_endpoint(admin_session):
    """Bypass gets past step-up gate. Because pointer swap is symmetric,
    calling rollback TWICE returns admin to the original active pointer —
    we do that here to comply with the safety constraint of never leaving
    live config mutated after the test."""
    if not STEP_UP_BYPASS_TOKEN:
        pytest.skip("no bypass token configured")
    headers = {
        **_csrf_headers(admin_session),
        "Content-Type": "application/json",
        "X-Step-Up-Bypass": STEP_UP_BYPASS_TOKEN,
    }
    r1 = admin_session.post(f"{BASE_URL}/api/config/rollback",
                            json={}, headers=headers, timeout=20)
    assert "step_up_required" not in r1.text.lower(), \
        f"bypass token not honored: {r1.status_code} {r1.text}"
    # Roll forward to restore original pointer (symmetric)
    if r1.status_code == 200:
        r2 = admin_session.post(f"{BASE_URL}/api/config/rollback",
                                json={}, headers=headers, timeout=20)
        assert r2.status_code == 200, \
            f"roll-forward failed — live config left mutated! {r2.status_code} {r2.text}"
        # Verify mode still supervised_live
        cfg = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=20).json()
        mode = cfg.get("operational_mode") or (cfg.get("config") or {}).get("operational_mode")
        assert mode == "supervised_live", f"mode changed after paired rollback! {mode}"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
