"""
iter-126 tests: OPTION A simplification — single-engine MTF cascade only.

Verifies:
  1. Backend health basic route.
  2. Admin login (cookie-based auth).
  3. GET /api/bot/config no longer exposes aggressive_mode / range_scalp_enabled /
     mtf_confluence_enabled / mtf_strict.
  4. PUT /api/bot/config accepts normal payloads; sending removed legacy fields
     is silently ignored (pydantic extras) and they are NOT persisted.
  5. GET /api/bot/mtf-confluence?symbol=XAUUSD returns cascade shape.
  6. GET /api/bot/pulse returns pulses; recent reasons show "MTF cascade:" HOLD.
  7. GET /api/bot/presets does NOT contain 'aggressive' preset or aggressive_mode field.
  8. GET /api/bot/health responds without server-side NameError (currently a known
     bug — tilt_cfgs undefined — test will document the failure).
  9. ai_signals.analyze_symbol returns dict with scope='mtf_confluence' and
     reasoning containing 'MTF' regardless of BUY/SELL/HOLD.
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import sys
import asyncio
import pytest
import requests
from live_target import require_live_base_url

BASE_URL = require_live_base_url()

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"

REMOVED_FIELDS = {"aggressive_mode", "range_scalp_enabled",
                  "mtf_confluence_enabled", "mtf_strict"}


# --- Fixtures ---------------------------------------------------------
@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW}, timeout=10)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    assert "access_token" in s.cookies, "no access_token cookie set"
    return s


# --- 1. Basic health --------------------------------------------------
def test_health_root_reachable():
    r = requests.get(f"{BASE_URL}/api/health", timeout=10)
    assert r.status_code in (200, 404), \
        f"expected 200 or 404 from /api/health, got {r.status_code}"
    # Try / as fallback
    if r.status_code == 404:
        r2 = requests.get(f"{BASE_URL}/api/", timeout=10)
        assert r2.status_code in (200, 404, 405)


# --- 2. Auth ----------------------------------------------------------
def test_admin_login_sets_cookie(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/auth/me", timeout=10)
    assert r.status_code == 200
    me = r.json()
    assert me.get("email") == ADMIN_EMAIL
    assert me.get("role") == "admin"


# --- 3. bot/config no longer exposes removed fields -------------------
def test_bot_config_no_legacy_fields(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=10)
    assert r.status_code == 200
    cfg = r.json()
    leaked = REMOVED_FIELDS.intersection(cfg.keys())
    assert not leaked, f"legacy fields still exposed in /bot/config: {leaked}"


# --- 4. PUT bot/config: normal payload works, legacy is ignored -------
def test_bot_config_put_normal_payload_succeeds(admin_session):
    # Read current trade_of_day_cap
    r0 = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=10)
    assert r0.status_code == 200
    original_cap = r0.json().get("trade_of_day_cap", 4)

    # Bump it to a distinctive test value then restore
    test_cap = 7 if original_cap != 7 else 8
    r1 = admin_session.put(f"{BASE_URL}/api/bot/config",
                           json={"trade_of_day_cap": test_cap}, timeout=10)
    assert r1.status_code == 200, f"PUT failed: {r1.status_code} {r1.text}"

    r2 = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=10)
    assert r2.status_code == 200
    assert r2.json().get("trade_of_day_cap") == test_cap

    # restore original
    admin_session.put(f"{BASE_URL}/api/bot/config",
                      json={"trade_of_day_cap": original_cap}, timeout=10)


def test_bot_config_put_legacy_fields_are_ignored(admin_session):
    # Send removed legacy fields — expect NO error and NOT persisted.
    payload = {"aggressive_mode": True, "range_scalp_enabled": True,
               "mtf_confluence_enabled": False, "mtf_strict": True}
    r = admin_session.put(f"{BASE_URL}/api/bot/config", json=payload, timeout=10)
    assert r.status_code in (200, 422), \
        f"PUT with legacy fields should return 200 (ignored) or 422 (rejected), got {r.status_code}: {r.text}"

    # Verify not persisted
    r2 = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=10)
    cfg = r2.json()
    persisted = {k: cfg.get(k) for k in REMOVED_FIELDS if k in cfg}
    assert not persisted, f"legacy fields persisted after PUT: {persisted}"


# --- 5. mtf-confluence cascade endpoint -------------------------------
def test_mtf_confluence_cascade_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/bot/mtf-confluence?symbol=XAUUSD",
                          timeout=15)
    assert r.status_code == 200, f"{r.status_code}: {r.text}"
    data = r.json()
    if data.get("available") is False:
        pytest.skip(f"M15 stream unavailable: {data.get('note')}")
    # Expected cascade keys
    for key in ("h4_trend", "h1_structure", "m15_setup", "aligned", "note"):
        assert key in data, f"missing '{key}' in cascade response: {list(data.keys())}"
    assert isinstance(data["aligned"], bool)


# --- 6. bot/pulse should reflect per-engine reasons (iter-127) --------
def test_bot_pulse_contains_per_engine_reasons(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/bot/pulse", timeout=15)
    assert r.status_code == 200
    payload = r.json()
    items = payload.get("items", []) if isinstance(payload, dict) else payload
    assert isinstance(items, list), f"pulse payload not iterable: {type(payload)}"
    assert len(items) > 0, "no bot_configs returned in pulse response"

    reasons = []
    for it in items:
        for slot in ("pulse", "notable"):
            v = it.get(slot) or {}
            r_str = str(v.get("reason", ""))
            if r_str:
                reasons.append(r_str)

    joined = " ".join(reasons).upper()
    # Any of the new engine labels should be present in some active account's reason
    engine_tokens = [
        "SNIPER", "BALANCED", "TREND RIDER", "SCALPER", "FAST SCALP",
        "BREAKOUT", "MEAN REVERSION", "MTF CASCADE",
    ]
    if not any(t in joined for t in engine_tokens) and \
            ("MARKET CLOSED" in joined or "COOLDOWN" in joined):
        pytest.skip("market closed / cooldown — engines idle, no labels emitted")
    assert any(t in joined for t in engine_tokens), (
        f"no per-engine label found in {len(reasons)} pulse reasons. "
        f"Sample: {reasons[:3]}"
    )


# --- 7. presets no longer contain 'aggressive' ------------------------
def test_bot_presets_no_aggressive(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/bot/presets", timeout=10)
    assert r.status_code == 200
    presets = r.json()
    # presets may be dict {key: {...}} or list [{key,...}]
    if isinstance(presets, dict):
        keys = list(presets.keys())
        configs = list(presets.values())
    else:
        keys = [p.get("key") or p.get("name") for p in presets]
        configs = [p.get("config") or p for p in presets]

    assert "aggressive" not in [str(k).lower() for k in keys], \
        f"'aggressive' preset still exists: keys={keys}"
    for c in configs:
        if isinstance(c, dict):
            assert "aggressive_mode" not in c, \
                f"preset still contains aggressive_mode: {c}"


# --- 8. bot/health endpoint ------------------------------------------
def test_bot_health_endpoint(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/bot/health-score", timeout=20)
    # This currently 500s due to `tilt_cfgs` NameError (aggressive_mode
    # section removed but variable still referenced in section 7 of
    # bot_health_score in bot_routes.py at line ~1198).
    assert r.status_code == 200, (
        f"GET /api/bot/health-score failed with {r.status_code}: {r.text[:400]} "
        "— likely NameError: tilt_cfgs not defined (see bot_routes.py:1198)"
    )
    data = r.json()
    assert "score" in data or "health_score" in data


# --- 9. ai_signals.analyze_symbol unit-level (iter-127) ---------------
def test_ai_signals_analyze_symbol_default_maps_to_moderate():
    """Default strategy=None → engine='mtf_moderate' scope."""
    sys.path.insert(0, _BACKEND_DIR)
    try:
        import ai_signals  # noqa
    except ImportError as e:
        pytest.skip(f"cannot import ai_signals: {e}")

    result = asyncio.run(ai_signals.analyze_symbol("XAUUSD", "medium"))
    assert isinstance(result, dict), f"expected dict, got {type(result)}"
    assert result.get("action") in ("BUY", "SELL", "HOLD"), \
        f"unexpected action: {result.get('action')}"
    # iter-127: default no-strategy → mtf_moderate engine
    assert result.get("scope") == "mtf_moderate", \
        f"scope must be 'mtf_moderate' (default), got {result.get('scope')}"
    assert result.get("strategy_engine") == "mtf_moderate"
    assert "engine_label" in result and result["engine_label"]
    reasoning = str(result.get("reasoning", ""))
    if "market closed" in reasoning.lower():
        pytest.skip("market closed — engine never ran, no label prefix")
    # reasoning must begin with the engine label
    assert reasoning.startswith(result["engine_label"]), \
        f"reasoning must start with engine label. reasoning={reasoning[:200]}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
