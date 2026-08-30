"""iter-127 · Per-account strategy engine dispatch.

Verifies:
  - strategy_engines.py pure functions (hf_scalp, range_fade, breakout).
  - resolve_engine mapping per preset key.
  - mtf_intraday MTF_MODES dict has strict/moderate/relaxed configs.
  - ai_signals.analyze_symbol('XAUUSD','medium',strategy=K) dispatches to
    correct engine for K in [None, sniper, balanced, trend_rider, scalper,
    fast_scalp, breakout, mean_reversion, custom:xyz]. Result dict includes
    scope==expected engine key + strategy_engine + engine_label; reasoning
    starts with the engine label; if scalp fires BUY/SELL, risk_pct_cap=0.25
    and SL/TP geometry is valid with rr_ratio >= 1.1.
  - HTTP: /api/bot/presets descriptions start with 'ENGINE:'; scalper cooldown=5,
    fast_scalp cooldown=3; no 'aggressive' preset.
  - HTTP: /api/bot/mtf-confluence?symbol=XAUUSD still 200 (strict by default).
  - HTTP: /api/bot/health-score returns 200 (regression from iter-41 fix).
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

sys.path.insert(0, _BACKEND_DIR)

from strategy_engines import (  # noqa: E402
    ENGINE_BY_PRESET, ENGINE_LABELS, MTF_MODE_BY_ENGINE,
    SCALP_ENGINES, SCALP_RISK_PCT_CAP, DETERMINISTIC_INTRADAY_SCOPES,
    resolve_engine, hf_scalp_signal, range_fade_signal, breakout_signal,
)
from mtf_intraday import MTF_MODES  # noqa: E402
from live_target import require_live_base_url

BASE_URL = require_live_base_url()

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"


# =========================================================================
# 1. strategy_engines constants + resolve_engine mapping
# =========================================================================
def test_engine_by_preset_mapping_is_complete():
    expected = {
        "sniper": "mtf_strict",
        "balanced": "mtf_moderate",
        "trend_rider": "mtf_relaxed",
        "scalper": "hf_scalp",
        "fast_scalp": "hf_scalp_fast",
        "breakout": "breakout_m15",
        "mean_reversion": "range_fade",
    }
    for k, v in expected.items():
        assert ENGINE_BY_PRESET.get(k) == v, f"{k} → expected {v}, got {ENGINE_BY_PRESET.get(k)}"


def test_resolve_engine_default_maps_to_mtf_moderate():
    assert resolve_engine(None) == "mtf_moderate"
    assert resolve_engine("") == "mtf_moderate"
    assert resolve_engine("custom:xyz") == "mtf_moderate"
    assert resolve_engine("unknown_key") == "mtf_moderate"
    assert resolve_engine("sniper") == "mtf_strict"
    assert resolve_engine("scalper") == "hf_scalp"
    assert resolve_engine("fast_scalp") == "hf_scalp_fast"
    assert resolve_engine("breakout") == "breakout_m15"
    assert resolve_engine("mean_reversion") == "range_fade"
    assert resolve_engine("trend_rider") == "mtf_relaxed"


def test_scalp_risk_pct_cap_is_quarter_pct():
    assert SCALP_RISK_PCT_CAP == 0.25
    assert "hf_scalp" in SCALP_ENGINES
    assert "hf_scalp_fast" in SCALP_ENGINES


def test_deterministic_scopes_are_the_intraday_engines():
    assert DETERMINISTIC_INTRADAY_SCOPES == {"hf_scalp", "hf_scalp_fast", "range_fade", "breakout_m15"}


def test_engine_labels_present():
    for eng in ("mtf_strict", "mtf_moderate", "mtf_relaxed",
                "hf_scalp", "hf_scalp_fast", "breakout_m15", "range_fade"):
        assert eng in ENGINE_LABELS and ENGINE_LABELS[eng]


# =========================================================================
# 2. Pure engine functions
# =========================================================================
def test_hf_scalp_buy_on_uptrend_burst():
    feats = {
        "atr15": 5, "trend": "UP", "last_price": 101, "ema20": 100,
        "ema20_slope_pct_2h": 0.2, "momentum_3h_pct": 0.2,
        "vwap_dist_pct": 0.5, "range_pos_pct": 50, "donchian20": "INSIDE",
    }
    sig, note = hf_scalp_signal(feats, fast=False)
    assert sig == "BUY", f"expected BUY, got {sig} / {note}"
    assert "momentum burst" in note.lower() or "vwap" in note.lower()


def test_hf_scalp_hold_when_flat():
    feats = {"atr15": 5, "trend": "FLAT", "last_price": 100, "ema20": 100}
    sig, note = hf_scalp_signal(feats)
    assert sig is None and "flat" in note.lower()


def test_hf_scalp_fast_softer_thresholds():
    """fast=True uses slope_min=0.05 vs 0.08 — a small burst should trip fast but not slow."""
    feats = {
        "atr15": 5, "trend": "UP", "last_price": 101, "ema20": 100,
        "ema20_slope_pct_2h": 0.06, "momentum_3h_pct": 0.07,
        "vwap_dist_pct": 1.0, "range_pos_pct": 50, "donchian20": "INSIDE",
    }
    sig_slow, _ = hf_scalp_signal(feats, fast=False)
    sig_fast, note_fast = hf_scalp_signal(feats, fast=True)
    assert sig_slow is None
    assert sig_fast == "BUY", f"fast should fire, got {sig_fast} / {note_fast}"


def test_range_fade_buy_at_range_low():
    feats = {
        "atr15": 2, "trend": "FLAT", "donchian20": "INSIDE",
        "session_high": 2050, "session_low": 2000, "range_pos_pct": 10,
        "day_range_pct": 1.5, "session_vwap": 2025,
    }
    sig, note = range_fade_signal(feats)
    assert sig == "BUY", f"expected BUY, got {sig} / {note}"
    assert "range fade" in note.lower()


def test_range_fade_none_when_trending():
    feats = {"atr15": 2, "trend": "UP", "donchian20": "INSIDE",
             "session_high": 2050, "session_low": 2000, "range_pos_pct": 10,
             "day_range_pct": 1.5, "session_vwap": 2025}
    sig, note = range_fade_signal(feats)
    assert sig is None and "not rangebound" in note.lower()


def test_breakout_buy_on_donchian_break_up():
    feats = {"atr15": 3, "donchian20": "BREAK_UP",
             "momentum_3h_pct": 0.5, "day_range_pct": 1.2}
    sig, note = breakout_signal(feats)
    assert sig == "BUY", f"expected BUY, got {sig} / {note}"


def test_breakout_none_inside_channel():
    feats = {"atr15": 3, "donchian20": "INSIDE",
             "momentum_3h_pct": 0.5, "day_range_pct": 1.2}
    sig, note = breakout_signal(feats)
    assert sig is None and "inside" in note.lower()


# =========================================================================
# 3. mtf_intraday MTF_MODES dict
# =========================================================================
def test_mtf_modes_dict_has_three_strictness_levels():
    assert set(MTF_MODES.keys()) == {"strict", "moderate", "relaxed"}
    assert MTF_MODES["strict"]["h4"] == "agree"
    assert MTF_MODES["moderate"]["h4"] == "non_opposing"
    assert MTF_MODES["relaxed"]["h4"] == "non_opposing"
    # relaxed uses wider pullback window
    assert MTF_MODES["relaxed"]["retrace_lo"] == 0.15
    assert MTF_MODES["relaxed"]["retrace_hi"] == 0.80
    # strict is tightest
    assert MTF_MODES["strict"]["retrace_lo"] >= MTF_MODES["moderate"]["retrace_lo"]


def test_moderate_allows_4h_flat_when_1h_trends():
    """moderate: 4H FLAT is OK if 1H is UP; strict rejects it."""
    from mtf_intraday import analyze_mtf_confluence
    # Build M15 bars where 4H comes out FLAT but 1H is UP.
    # Approach: 4H needs a long-history flat pattern, 1H last 5-6 hours = strong up.
    # M15 spacing = 900s. 4H = 16 bars. Give first 400 bars alternating (flat 4H),
    # then last ~25 bars steep up (creates UP 1H).
    bars = []
    for i in range(400):
        c = 100 + (0.5 if i % 2 else -0.5)
        bars.append({"t": i * 900, "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 100})
    for j in range(25):
        c = 100 + j * 3
        bars.append({"t": (400 + j) * 900, "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 100})
    rep_mod = analyze_mtf_confluence(bars, live_price=200, atr15=3.0, mode="moderate")
    rep_strict = analyze_mtf_confluence(bars, live_price=200, atr15=3.0, mode="strict")
    # We only care that moderate DOES NOT reject on "no higher-TF agreement"
    # while strict DOES when 4H disagrees/flat.
    if rep_strict["h4_trend"] == "FLAT" and rep_strict["h1_structure"] == "UP":
        assert "no higher-TF agreement" in rep_strict["note"], rep_strict["note"]
        # Moderate should not use the strict rejection message
        assert "no higher-TF agreement" not in rep_mod["note"]


# =========================================================================
# 4. ai_signals.analyze_symbol dispatch across all presets
# =========================================================================
@pytest.mark.parametrize("preset,expected_scope", [
    (None, "mtf_moderate"),
    ("sniper", "mtf_strict"),
    ("balanced", "mtf_moderate"),
    ("trend_rider", "mtf_relaxed"),
    ("scalper", "hf_scalp"),
    ("fast_scalp", "hf_scalp_fast"),
    ("breakout", "breakout_m15"),
    ("mean_reversion", "range_fade"),
    ("custom:xyz", "mtf_moderate"),
])
def test_analyze_symbol_dispatches_per_preset(preset, expected_scope):
    import ai_signals
    result = asyncio.run(ai_signals.analyze_symbol("XAUUSD", "medium", 0, preset))
    assert isinstance(result, dict)
    assert result.get("action") in ("BUY", "SELL", "HOLD"), result.get("action")
    assert result.get("scope") == expected_scope, (
        f"preset={preset} → scope={result.get('scope')}, expected {expected_scope}"
    )
    assert result.get("strategy_engine") == expected_scope
    assert result.get("engine_label"), "engine_label missing"
    reasoning = str(result.get("reasoning", ""))
    if "market closed" in reasoning.lower():
        pytest.skip("market closed — engine never ran, no label prefix")
    assert reasoning.startswith(result["engine_label"]), (
        f"reasoning must start with engine label '{result['engine_label']}'. "
        f"reasoning={reasoning[:200]}"
    )
    # If a scalp fires BUY/SELL, verify risk_pct_cap + SL/TP geometry
    if expected_scope in ("hf_scalp", "hf_scalp_fast") and result["action"] in ("BUY", "SELL"):
        assert result.get("risk_pct_cap") == 0.25, \
            f"scalp fired but risk_pct_cap != 0.25: {result.get('risk_pct_cap')}"
        assert result.get("rr_ratio") is not None and result["rr_ratio"] >= 1.1
        # SL/TP geometry
        entry = result["entry_price"]; sl = result["stop_loss"]
        tp1, tp3 = result["tp1"], result["tp3"]
        if result["action"] == "BUY":
            assert sl < entry and tp1 > entry and tp3 > tp1
        else:
            assert sl > entry and tp1 < entry and tp3 < tp1


# =========================================================================
# 5. HTTP endpoints
# =========================================================================
@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW}, timeout=10)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return s


def test_presets_have_engine_prefix_and_correct_cooldowns(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/bot/presets", timeout=10)
    assert r.status_code == 200
    presets = r.json()
    # /api/bot/presets returns {"presets": [...]} or a bare list; handle both.
    if isinstance(presets, dict) and "presets" in presets:
        preset_list = presets["presets"]
    elif isinstance(presets, dict):
        preset_list = [{"key": k, **(v if isinstance(v, dict) else {})} for k, v in presets.items()]
    else:
        preset_list = presets

    keys = {p.get("key"): p for p in preset_list}
    assert "aggressive" not in keys, "aggressive preset should be gone"

    # Every preset description should start with 'ENGINE:'
    for k, p in keys.items():
        desc = str(p.get("description", ""))
        assert desc.startswith("ENGINE:"), \
            f"preset '{k}' description does not start with 'ENGINE:': {desc[:60]}"

    # scalper: signal_cooldown_minutes=5, fast_scalp: 3
    scalper_cfg = keys["scalper"].get("config", {})
    fast_cfg = keys["fast_scalp"].get("config", {})
    assert scalper_cfg.get("signal_cooldown_minutes") == 5, \
        f"scalper cooldown expected 5, got {scalper_cfg.get('signal_cooldown_minutes')}"
    assert fast_cfg.get("signal_cooldown_minutes") == 3, \
        f"fast_scalp cooldown expected 3, got {fast_cfg.get('signal_cooldown_minutes')}"


def test_mtf_confluence_endpoint_ok(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/bot/mtf-confluence?symbol=XAUUSD",
                          timeout=15)
    assert r.status_code == 200, r.text
    data = r.json()
    if data.get("available") is False:
        pytest.skip(f"M15 stream unavailable: {data.get('note')}")
    for key in ("h4_trend", "h1_structure", "aligned", "note"):
        assert key in data


def test_health_score_regression_iter41_fix(admin_session):
    """Verify iter-41 tilt_cfgs NameError is fixed — endpoint returns 200."""
    r = admin_session.get(f"{BASE_URL}/api/bot/health-score", timeout=25)
    assert r.status_code == 200, (
        f"GET /api/bot/health-score failed with {r.status_code}: {r.text[:400]}"
    )
    data = r.json()
    assert "score" in data or "health_score" in data


def test_bot_pulse_per_account_engine_label(admin_session):
    """Each active account's pulse reason should begin with its own engine label."""
    r = admin_session.get(f"{BASE_URL}/api/bot/pulse", timeout=20)
    assert r.status_code == 200
    payload = r.json()
    items = payload.get("items", []) if isinstance(payload, dict) else payload
    assert isinstance(items, list) and len(items) > 0

    # Only inspect active bots; heartbeating accounts should have per-engine reasons.
    active_items = [it for it in items if it.get("active")]
    if not active_items:
        pytest.skip("No active accounts to check per-engine labels on")

    all_engine_tokens = ["SNIPER", "BALANCED", "TREND RIDER", "SCALPER",
                        "FAST SCALP", "BREAKOUT", "MEAN REVERSION"]
    matched = 0
    for it in active_items:
        pulse = it.get("pulse") or {}
        notable = it.get("notable") or {}
        # Check BOTH reasons: transient cooldown SKIPs legitimately carry no
        # engine label, but the notable (last decision) reason does.
        reason = (str(pulse.get("reason") or "") + " "
                  + str(notable.get("reason") or "")).upper()
        if any(tok in reason for tok in all_engine_tokens):
            matched += 1
    if matched == 0:
        joined_all = " ".join(
            str((it.get("pulse") or {}).get("reason") or "") + " "
            + str((it.get("notable") or {}).get("reason") or "")
            for it in active_items).upper()
        if ("MARKET CLOSED" in joined_all or "COOLDOWN" in joined_all
                or not joined_all.strip()):
            pytest.skip("market closed / cooldown / no reasons yet — "
                        "engines idle, no labels emitted")
    # At least one active account should show an engine label in its reason
    assert matched > 0, "no per-engine label found on any active-account pulse"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
