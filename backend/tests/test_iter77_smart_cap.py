"""iter-77 · Smart Cap regression tests.

Smart Cap: when `profit_taking_mode == "win_rate"` and the user has NOT
set an explicit per-symbol cap, the 100p default cap fires ONLY in
choppy regimes (CAUTIOUS_WAIT / DEFENSIVE_SCALP / TRANSITIONAL / RANGING).
TRENDING and AGGRESSIVE regimes have the cap REMOVED so runners stretch.

Explicit per-symbol caps via `max_tp_pips_per_symbol` always win — the
user can force a hard ceiling regardless of regime."""
from __future__ import annotations

import pytest

from adaptive_mode import (
    apply_profit_taking_mode,
    DEFAULT_WIN_RATE_TP_CAP_PIPS,
    CHOPPY_REGIMES,
    TRENDING_REGIMES,
)


def _xauusd_signal(action="BUY", entry=4000.0, tp_distance_pips=300, regime_exec=None):
    pip_size = 0.1
    direction = 1 if action == "BUY" else -1
    tp = entry + direction * tp_distance_pips * pip_size
    return {
        "symbol": "XAUUSD", "action": action,
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


# ─────────────── Smart Cap: TRENDING → no cap ───────────────

def test_smart_cap_trending_regime_lets_winners_run():
    """TRENDING regime → win_rate mode should NOT clip the TP. Profit
    can stretch beyond 100p just like Plan A."""
    sig = _xauusd_signal("BUY", entry=4000.0, tp_distance_pips=300,
                         regime_exec="TRENDING")
    new_sig, _ = apply_profit_taking_mode(sig, {"profit_taking_mode": "win_rate"})
    # No clip → TP stays at original 4030.0 (300 pips)
    assert new_sig["take_profit"] == pytest.approx(4030.0, abs=0.01)
    apt = new_sig["adaptive_profit_taking"]
    assert apt["tp_cap_pips"] is None
    assert apt["smart_cap_applied"] is False
    assert apt["smart_cap_skipped_for_trend"] is True


def test_smart_cap_aggressive_regime_lets_winners_run():
    sig = _xauusd_signal("SELL", entry=4000.0, tp_distance_pips=400,
                         regime_exec="AGGRESSIVE")
    new_sig, _ = apply_profit_taking_mode(sig, {"profit_taking_mode": "win_rate"})
    # SELL: TP should be at 4000 - 400×0.1 = 3960. Unchanged.
    assert new_sig["take_profit"] == pytest.approx(3960.0, abs=0.01)
    assert new_sig["adaptive_profit_taking"]["smart_cap_skipped_for_trend"] is True


# ─────────────── Smart Cap: CHOPPY → cap applied ───────────────

def test_smart_cap_transitional_regime_applies_100p():
    """TRANSITIONAL regime is choppy — cap fires."""
    sig = _xauusd_signal("BUY", entry=4000.0, tp_distance_pips=300,
                         regime_exec="TRANSITIONAL")
    new_sig, _ = apply_profit_taking_mode(sig, {"profit_taking_mode": "win_rate"})
    assert new_sig["take_profit"] == pytest.approx(4010.0, abs=0.01)
    apt = new_sig["adaptive_profit_taking"]
    assert apt["smart_cap_applied"] is True
    assert apt["smart_cap_skipped_for_trend"] is False
    assert apt["tp_cap_pips"] == DEFAULT_WIN_RATE_TP_CAP_PIPS  # 100p
    assert apt["regime_tightened"] is False  # TRANSITIONAL isn't in the 0.6× bucket


def test_smart_cap_cautious_wait_tightens_to_60p():
    """CAUTIOUS_WAIT is the choppiest bucket — 100p × 0.6 = 60p."""
    sig = _xauusd_signal("BUY", entry=4000.0, tp_distance_pips=300,
                         regime_exec="CAUTIOUS_WAIT")
    new_sig, _ = apply_profit_taking_mode(sig, {"profit_taking_mode": "win_rate"})
    assert new_sig["take_profit"] == pytest.approx(4006.0, abs=0.01)
    apt = new_sig["adaptive_profit_taking"]
    assert apt["smart_cap_applied"] is True
    assert apt["regime_tightened"] is True
    assert apt["tp_cap_pips"] == pytest.approx(60.0, abs=0.1)


def test_smart_cap_defensive_scalp_tightens_to_60p():
    sig = _xauusd_signal("BUY", entry=4000.0, tp_distance_pips=300,
                         regime_exec="DEFENSIVE_SCALP")
    new_sig, _ = apply_profit_taking_mode(sig, {"profit_taking_mode": "win_rate"})
    assert new_sig["take_profit"] == pytest.approx(4006.0, abs=0.01)
    assert new_sig["adaptive_profit_taking"]["regime_tightened"] is True


def test_smart_cap_ranging_applies_default_no_tightening():
    """RANGING is choppy → 100p cap, but NOT inside the 60p tightening bucket."""
    sig = _xauusd_signal("BUY", entry=4000.0, tp_distance_pips=300,
                         regime_exec="RANGING")
    new_sig, _ = apply_profit_taking_mode(sig, {"profit_taking_mode": "win_rate"})
    assert new_sig["take_profit"] == pytest.approx(4010.0, abs=0.01)
    apt = new_sig["adaptive_profit_taking"]
    assert apt["smart_cap_applied"] is True
    assert apt["regime_tightened"] is False


# ─────────── Explicit per-symbol cap always wins ───────────

def test_explicit_cap_always_applies_in_trending():
    """Even in TRENDING (where Smart Cap would skip), a user-set per-symbol
    cap MUST still clip the TP — user-explicit always wins."""
    sig = _xauusd_signal("BUY", entry=4000.0, tp_distance_pips=300,
                         regime_exec="TRENDING")
    cfg = {"profit_taking_mode": "win_rate",
           "max_tp_pips_per_symbol": {"XAUUSD": 50}}
    new_sig, _ = apply_profit_taking_mode(sig, cfg)
    assert new_sig["take_profit"] == pytest.approx(4005.0, abs=0.01)
    apt = new_sig["adaptive_profit_taking"]
    assert apt["tp_cap_pips"] == 50.0  # honored
    assert apt["smart_cap_applied"] is False  # this was an EXPLICIT cap, not Smart


def test_explicit_cap_still_tightened_in_cautious_wait():
    """Explicit cap + CAUTIOUS_WAIT → tightening still applies on top
    (50p × 0.6 = 30p) because the regime context is independent."""
    sig = _xauusd_signal("BUY", entry=4000.0, tp_distance_pips=300,
                         regime_exec="CAUTIOUS_WAIT")
    cfg = {"profit_taking_mode": "win_rate",
           "max_tp_pips_per_symbol": {"XAUUSD": 50}}
    new_sig, _ = apply_profit_taking_mode(sig, cfg)
    assert new_sig["take_profit"] == pytest.approx(4003.0, abs=0.01)
    assert new_sig["adaptive_profit_taking"]["regime_tightened"] is True


# ─────────── Unknown / missing regime — be conservative ───────────

def test_smart_cap_no_regime_context_no_cap():
    """No regime info → DON'T clip. Better to let trades run than to cap
    them based on an unknown regime."""
    sig = _xauusd_signal("BUY", entry=4000.0, tp_distance_pips=300,
                         regime_exec=None)
    new_sig, _ = apply_profit_taking_mode(sig, {"profit_taking_mode": "win_rate"})
    # No clip — original TP stays
    assert new_sig["take_profit"] == pytest.approx(4030.0, abs=0.01)
    apt = new_sig["adaptive_profit_taking"]
    assert apt["smart_cap_applied"] is False
    assert apt["tp_cap_pips"] is None


# ─────────── Regime constants sanity ───────────

def test_regime_buckets_disjoint_and_well_formed():
    """CHOPPY and TRENDING sets must not overlap."""
    assert CHOPPY_REGIMES.isdisjoint(TRENDING_REGIMES)
    assert "CAUTIOUS_WAIT" in CHOPPY_REGIMES
    assert "TRENDING" in TRENDING_REGIMES
    assert "AGGRESSIVE" in TRENDING_REGIMES


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
