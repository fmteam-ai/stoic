"""Tests for iter-74 — Win-Rate Adaptive Mode (Phases 1-3).

Phase 1  · profit_taking_mode + max_tp_pips_per_symbol + TP clipping
Phase 2  · rolling adaptive risk multiplier
Phase 3  · regime-aware auto-preset selection
"""
from __future__ import annotations
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
from datetime import datetime, timezone, timedelta

import pytest
import requests
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from adaptive_mode import (
    apply_profit_taking_mode,
    pick_preset_for_regime,
    apply_auto_preset,
    _clip_tp_to_pip_cap,
    _multiplier_for,
    PROFIT_TAKING_OVERLAYS,
    DEFAULT_WIN_RATE_TP_CAP_PIPS,
    CHOPPY_REGIME_TP_MULTIPLIER,
)


# ───────────────────────── helpers ─────────────────────────

BASE_URL = "https://stoic-trading-bot.preview.emergentagent.com"
if "REACT_APP_BACKEND_URL" not in os.environ:
    try:
        with open(_os.path.join(_REPO_DIR, "frontend", ".env")) as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL"):
                    BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
                    break
    except Exception:
        pass

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


def _get_db_url():
    mongo_url = "mongodb://localhost:27017"
    db_name = "test_database"
    try:
        with open(_os.path.join(_BACKEND_DIR, ".env")) as f:
            for line in f:
                if line.startswith("MONGO_URL="):
                    mongo_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("DB_NAME="):
                    db_name = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return mongo_url, db_name


def _xauusd_signal(action="BUY", entry=4000.0, tp_distance_pips=200.0,
                   regime_exec=None) -> dict:
    """Build a synthetic XAUUSD signal at the given entry with TP at
    `tp_distance_pips` away in the direction of action. For XAUUSD the
    pip size is 0.1 (so 200 pips = 20.0 price units)."""
    pip_size = 0.1  # XAUUSD pip
    direction = 1 if action == "BUY" else -1
    tp = entry + direction * tp_distance_pips * pip_size
    return {
        "symbol": "XAUUSD",
        "action": action,
        "entry_price": entry,
        "stop_loss": entry - direction * 100 * pip_size,
        "take_profit": tp,
        "tp1": entry + direction * (tp_distance_pips * 0.4) * pip_size,
        "tp2": entry + direction * (tp_distance_pips * 0.7) * pip_size,
        "tp3": tp,
        "tp_pips": [tp_distance_pips * 0.4, tp_distance_pips * 0.7, tp_distance_pips],
        "confidence": 75,
        "regime_execution_mode": {"execution_mode": regime_exec} if regime_exec else {},
    }


# ════════════════════════ Phase 1 — TP clipping ════════════════════════

def test_clip_buy_tp_to_cap():
    """BUY with TP 200p out should clip to 100p when cap=100."""
    sig = _xauusd_signal("BUY", entry=4000.0, tp_distance_pips=200)
    clipped = _clip_tp_to_pip_cap(sig, cap_pips=100.0)
    assert clipped["take_profit"] == pytest.approx(4010.0, abs=0.01)
    assert clipped["tp3"] == pytest.approx(4010.0, abs=0.01)
    # tp1 (80p) and tp2 (140p) — tp1 stays, tp2 is also clipped.
    assert clipped["tp_pips"][0] == pytest.approx(80.0, abs=0.1)
    assert clipped["tp_pips"][2] == pytest.approx(100.0, abs=0.1)


def test_clip_sell_tp_to_cap():
    """SELL with TP 200p below should clip upward (= closer to entry)."""
    sig = _xauusd_signal("SELL", entry=4000.0, tp_distance_pips=200)
    clipped = _clip_tp_to_pip_cap(sig, cap_pips=100.0)
    assert clipped["take_profit"] == pytest.approx(3990.0, abs=0.01)


def test_clip_noop_when_inside_cap():
    """TP already within cap → no change."""
    sig = _xauusd_signal("BUY", entry=4000.0, tp_distance_pips=50)
    clipped = _clip_tp_to_pip_cap(sig, cap_pips=100.0)
    assert clipped["take_profit"] == pytest.approx(sig["take_profit"], abs=0.01)


def test_clip_hold_signal_passthrough():
    """No SL/TP on HOLD → function returns untouched."""
    sig = {"symbol": "XAUUSD", "action": "HOLD", "entry_price": None, "take_profit": None}
    assert _clip_tp_to_pip_cap(sig, cap_pips=100.0) is sig


# ──────────── profit_taking_mode end-to-end ────────────

def test_expected_value_mode_no_mutation():
    """Default mode → no overlay applied, signal TP unchanged."""
    sig = _xauusd_signal("BUY", entry=4000.0, tp_distance_pips=200)
    cfg = {"profit_taking_mode": "expected_value"}
    new_sig, eff_cfg = apply_profit_taking_mode(sig, cfg)
    # TP unchanged
    assert new_sig["take_profit"] == pytest.approx(sig["take_profit"], abs=0.01)
    # No overlay added
    assert "partial_close_trigger_r" not in eff_cfg or eff_cfg.get("partial_close_trigger_r") is None
    # Telemetry attached
    assert new_sig["adaptive_profit_taking"]["mode"] == "expected_value"
    assert new_sig["adaptive_profit_taking"]["tp_cap_pips"] is None


def test_win_rate_mode_applies_default_100p_cap():
    """win_rate mode + no per-symbol cap + CHOPPY regime → 100p default cap.
    iter-77 Smart Cap: the default cap now requires a choppy regime context."""
    sig = _xauusd_signal("BUY", entry=4000.0, tp_distance_pips=300,
                         regime_exec="TRANSITIONAL")
    cfg = {"profit_taking_mode": "win_rate"}
    new_sig, eff_cfg = apply_profit_taking_mode(sig, cfg)
    assert new_sig["take_profit"] == pytest.approx(4010.0, abs=0.01)
    # Overlay applied
    assert eff_cfg["partial_close_trigger_r"] == 0.5
    assert eff_cfg["partial_close_fraction"] == 0.7
    assert eff_cfg["trailing_start_r"] == 0.5
    assert eff_cfg["trailing_distance_r"] == 0.25
    assert new_sig["adaptive_profit_taking"]["tp_cap_pips"] == DEFAULT_WIN_RATE_TP_CAP_PIPS
    assert new_sig["adaptive_profit_taking"]["smart_cap_applied"] is True


def test_win_rate_explicit_cap_overrides_default():
    """User's per-symbol cap takes precedence over the 100p default."""
    sig = _xauusd_signal("BUY", entry=4000.0, tp_distance_pips=300)
    cfg = {"profit_taking_mode": "win_rate",
           "max_tp_pips_per_symbol": {"XAUUSD": 50}}
    new_sig, _ = apply_profit_taking_mode(sig, cfg)
    assert new_sig["take_profit"] == pytest.approx(4005.0, abs=0.01)
    assert new_sig["adaptive_profit_taking"]["tp_cap_pips"] == 50


def test_choppy_regime_tightens_cap():
    """In DEFENSIVE_SCALP regime, cap is multiplied by 0.6 → 100p → 60p."""
    sig = _xauusd_signal("BUY", entry=4000.0, tp_distance_pips=300,
                         regime_exec="DEFENSIVE_SCALP")
    cfg = {"profit_taking_mode": "win_rate"}
    new_sig, _ = apply_profit_taking_mode(sig, cfg)
    assert new_sig["take_profit"] == pytest.approx(4006.0, abs=0.01)
    assert new_sig["adaptive_profit_taking"]["regime_tightened"] is True
    assert new_sig["adaptive_profit_taking"]["tp_cap_pips"] == pytest.approx(60.0, abs=0.1)


def test_trend_follow_mode_no_cap_by_default():
    """trend_follow widens partial-close knobs but does NOT cap TP."""
    sig = _xauusd_signal("BUY", entry=4000.0, tp_distance_pips=300)
    cfg = {"profit_taking_mode": "trend_follow"}
    new_sig, eff_cfg = apply_profit_taking_mode(sig, cfg)
    assert new_sig["take_profit"] == pytest.approx(sig["take_profit"], abs=0.01)
    assert eff_cfg["partial_close_trigger_r"] == 1.5
    assert eff_cfg["trailing_start_r"] == 2.0
    assert new_sig["adaptive_profit_taking"]["tp_cap_pips"] is None


def test_per_symbol_cap_works_in_expected_value_mode():
    """User can clip TPs even without switching mode."""
    sig = _xauusd_signal("BUY", entry=4000.0, tp_distance_pips=300)
    cfg = {"profit_taking_mode": "expected_value",
           "max_tp_pips_per_symbol": {"XAUUSD": 75}}
    new_sig, _ = apply_profit_taking_mode(sig, cfg)
    assert new_sig["take_profit"] == pytest.approx(4007.5, abs=0.01)


# ════════════════════════ Phase 2 — Risk multiplier ════════════════════

def test_multiplier_buckets():
    """Multiplier table is monotonic and matches design."""
    assert _multiplier_for(80) == 1.3
    assert _multiplier_for(70) == 1.3
    assert _multiplier_for(65) == 1.15
    assert _multiplier_for(55) == 1.0
    assert _multiplier_for(45) == 0.7
    assert _multiplier_for(30) == 0.5
    assert _multiplier_for(0) == 0.5


@pytest.mark.asyncio
async def test_risk_multiplier_neutral_when_few_samples():
    """With < 5 closed trades, multiplier stays at 1.0."""
    from adaptive_mode import compute_risk_multiplier
    mongo_url, db_name = _get_db_url()
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    # Patch database._db so compute_risk_multiplier uses our client
    import database as _database
    _orig_db = _database._db
    _database._db = db
    uid = "test-user-74-few"
    try:
        await db.trades.delete_many({"user_id": uid})
        for i in range(3):
            await db.trades.insert_one({
                "user_id": uid, "status": "closed", "origin": "auto",
                "pnl": 10.0,
                "closed_at": (datetime.now(timezone.utc) - timedelta(hours=i)).isoformat(),
            })
        info = await compute_risk_multiplier(uid, window=20)
        assert info["multiplier"] == 1.0
        assert info["samples"] == 3
        assert "Insufficient data" in info["reason"]
    finally:
        await db.trades.delete_many({"user_id": uid})
        _database._db = _orig_db
        client.close()


@pytest.mark.asyncio
async def test_risk_multiplier_scales_with_win_rate():
    """6 wins / 4 losses (60%) → 1.15× multiplier."""
    from adaptive_mode import compute_risk_multiplier
    mongo_url, db_name = _get_db_url()
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    import database as _database
    _orig_db = _database._db
    _database._db = db
    uid = "test-user-74-mid"
    try:
        await db.trades.delete_many({"user_id": uid})
        for i in range(6):
            await db.trades.insert_one({
                "user_id": uid, "status": "closed", "origin": "auto",
                "pnl": 20.0,
                "closed_at": (datetime.now(timezone.utc) - timedelta(hours=i)).isoformat(),
            })
        for i in range(4):
            await db.trades.insert_one({
                "user_id": uid, "status": "closed", "origin": "auto",
                "pnl": -10.0,
                "closed_at": (datetime.now(timezone.utc) - timedelta(hours=i+10)).isoformat(),
            })
        info = await compute_risk_multiplier(uid, window=20)
        assert info["samples"] == 10
        assert info["win_rate_pct"] == 60.0
        assert info["multiplier"] == 1.15
    finally:
        await db.trades.delete_many({"user_id": uid})
        _database._db = _orig_db
        client.close()


@pytest.mark.asyncio
async def test_risk_multiplier_protects_after_losing_streak():
    """3 wins / 7 losses (30%) → 0.5× — capital-protect mode."""
    from adaptive_mode import compute_risk_multiplier
    mongo_url, db_name = _get_db_url()
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    import database as _database
    _orig_db = _database._db
    _database._db = db
    uid = "test-user-74-bad"
    try:
        await db.trades.delete_many({"user_id": uid})
        for i in range(3):
            await db.trades.insert_one({
                "user_id": uid, "status": "closed", "origin": "auto",
                "pnl": 10.0,
                "closed_at": (datetime.now(timezone.utc) - timedelta(hours=i)).isoformat(),
            })
        for i in range(7):
            await db.trades.insert_one({
                "user_id": uid, "status": "closed", "origin": "auto",
                "pnl": -5.0,
                "closed_at": (datetime.now(timezone.utc) - timedelta(hours=i+10)).isoformat(),
            })
        info = await compute_risk_multiplier(uid, window=20)
        assert info["samples"] == 10
        assert info["win_rate_pct"] == 30.0
        assert info["multiplier"] == 0.5
    finally:
        await db.trades.delete_many({"user_id": uid})
        _database._db = _orig_db
        client.close()


# ════════════════════════ Phase 3 — Auto-preset ════════════════════════

def test_pick_preset_aggressive_to_trend_rider():
    pick = pick_preset_for_regime("AGGRESSIVE")
    assert pick["preset_key"] == "trend_rider"


def test_pick_preset_defensive_to_fast_scalp():
    pick = pick_preset_for_regime("DEFENSIVE_SCALP")
    assert pick["preset_key"] == "fast_scalp"


def test_pick_preset_cautious_wait_to_scalper():
    pick = pick_preset_for_regime("CAUTIOUS_WAIT")
    assert pick["preset_key"] == "scalper"


def test_pick_preset_falls_back_to_balanced():
    pick = pick_preset_for_regime(None, regime=None)
    assert pick["preset_key"] == "balanced"


def test_pick_preset_unknown_mode_falls_back():
    pick = pick_preset_for_regime("WAT_IS_THIS")
    assert pick["preset_key"] == "balanced"


def test_apply_auto_preset_disabled_returns_cfg_unchanged():
    """When auto_preset_enabled=False, the function is a passthrough."""
    cfg = {"auto_preset_enabled": False, "min_confidence_override": 80}
    sig = _xauusd_signal(regime_exec="AGGRESSIVE")
    out_cfg, info = apply_auto_preset(cfg, sig)
    assert out_cfg is cfg
    assert info["enabled"] is False


def test_apply_auto_preset_overlays_trend_rider_in_aggressive():
    """Enabled + AGGRESSIVE regime → trend_rider config knobs overlay cfg."""
    cfg = {"auto_preset_enabled": True, "min_confidence_override": 80,
           "risk_level": "medium"}
    sig = _xauusd_signal(regime_exec="AGGRESSIVE")
    out_cfg, info = apply_auto_preset(cfg, sig)
    assert info["enabled"] is True
    assert info["selected_preset"] == "trend_rider"
    # trend_rider's knobs are now in out_cfg
    assert out_cfg["min_confidence_override"] == 68  # from preset
    assert out_cfg["trailing_start_r"] == 1.5
    assert out_cfg["active_preset"] == "trend_rider_auto"
    # risk_level untouched (presets never override it)
    assert out_cfg["risk_level"] == "medium"


def test_apply_auto_preset_overlays_fast_scalp_in_defensive():
    cfg = {"auto_preset_enabled": True}
    sig = _xauusd_signal(regime_exec="DEFENSIVE_SCALP")
    out_cfg, info = apply_auto_preset(cfg, sig)
    assert info["selected_preset"] == "fast_scalp"
    # fast_scalp's signature knobs
    assert out_cfg["partial_close_trigger_r"] == 0.5
    assert out_cfg["partial_close_fraction"] == 0.7
    assert out_cfg["profit_taking_mode"] == "win_rate"
    assert out_cfg["max_tp_pips_per_symbol"] == {"XAUUSD": 100, "BTCUSD": 100}


# ════════════════════════ HTTP integration ════════════════════════════

@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return s


def test_patch_config_accepts_new_adaptive_fields(admin_session):
    """PATCH /api/bot/config can set the new iter-74 fields."""
    body = {
        "profit_taking_mode": "win_rate",
        "max_tp_pips_per_symbol": {"XAUUSD": 100, "BTCUSD": 100},
        "adaptive_risk_enabled": True,
        "adaptive_risk_window": 25,
        "auto_preset_enabled": False,  # don't enable on live admin acct
    }
    r = admin_session.put(f"{BASE_URL}/api/bot/config", json=body, timeout=15)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    # Read back
    g = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=15)
    assert g.status_code == 200
    cfg = g.json()
    assert cfg["profit_taking_mode"] == "win_rate"
    assert cfg["max_tp_pips_per_symbol"] == {"XAUUSD": 100.0, "BTCUSD": 100.0}
    assert cfg["adaptive_risk_enabled"] is True
    assert cfg["adaptive_risk_window"] == 25
    assert cfg["auto_preset_enabled"] is False

    # Restore defaults so we don't pollute downstream tests
    restore = {
        "profit_taking_mode": "expected_value",
        "max_tp_pips_per_symbol": {},
        "adaptive_risk_enabled": False,
        "adaptive_risk_window": 20,
    }
    admin_session.put(f"{BASE_URL}/api/bot/config", json=restore, timeout=15)


def test_patch_config_coerces_garbage_profit_taking_mode(admin_session):
    """Invalid mode string falls back to 'expected_value'."""
    r = admin_session.put(f"{BASE_URL}/api/bot/config",
                          json={"profit_taking_mode": "TURBOSHRED"}, timeout=15)
    assert r.status_code == 200
    g = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=15)
    assert g.json()["profit_taking_mode"] == "expected_value"


def test_patch_config_clamps_adaptive_risk_window(admin_session):
    """Window value out of [5, 200] gets clamped."""
    admin_session.put(f"{BASE_URL}/api/bot/config",
                      json={"adaptive_risk_window": 999}, timeout=15)
    g = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=15)
    assert g.json()["adaptive_risk_window"] == 200
    admin_session.put(f"{BASE_URL}/api/bot/config",
                      json={"adaptive_risk_window": 1}, timeout=15)
    g = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=15)
    assert g.json()["adaptive_risk_window"] == 5
    # restore
    admin_session.put(f"{BASE_URL}/api/bot/config",
                      json={"adaptive_risk_window": 20}, timeout=15)


def test_adaptive_status_endpoint_returns_full_payload(admin_session):
    """GET /api/bot/adaptive-status returns all 3 phases' status."""
    r = admin_session.get(f"{BASE_URL}/api/bot/adaptive-status", timeout=15)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    data = r.json()
    assert "profit_taking_mode" in data
    assert "max_tp_pips_per_symbol" in data
    assert "adaptive_risk" in data
    assert "auto_preset" in data
    # auto_preset.would_select must be one of the known presets
    assert data["auto_preset"]["would_select"] in {
        "trend_rider", "fast_scalp", "scalper", "mean_reversion", "balanced",
    }


def test_presets_endpoint_exposes_fast_scalp(admin_session):
    """The new fast_scalp preset shows up in /api/bot/presets."""
    r = admin_session.get(f"{BASE_URL}/api/bot/presets", timeout=15)
    assert r.status_code == 200
    keys = {p["key"] for p in r.json()["presets"]}
    assert "fast_scalp" in keys


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
