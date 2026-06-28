"""Tests for iter-67 Multi-Timeframe (MTF) tier pack + gate.

Verifies:
  - compute_mtf_tiers returns SHORT/MEDIUM/LONG with valid directions
  - empty / short history → graceful FLAT
  - clear uptrend / downtrend synthetic data → tiers agree UP / DOWN
  - alignment counters + dominant trend
  - mtf_check.multi_timeframe_gate uses tiered path when ready
  - gate falls back to legacy slope path when tiers not ready
  - counter-trend BUY/SELL vetoed (>=2 tiers disagree)
  - HOLD never vetoed
"""
from __future__ import annotations

from mtf_tiers import compute_mtf_tiers
from mtf_check import multi_timeframe_gate


def _gen_history(prices: list) -> list:
    """Build minimal OHLC dicts from a closes list."""
    return [{"close": p, "open": p, "high": p, "low": p, "volume": 0} for p in prices]


# ─────────────── compute_mtf_tiers ───────────────
def test_empty_history_returns_flat():
    out = compute_mtf_tiers([])
    assert out["ready"] is False
    for k in ("SHORT", "MEDIUM", "LONG"):
        assert out[k]["direction"] == "FLAT"
        assert out[k]["ready"] is False
    assert out["alignment"]["dominant"] == "MIXED"


def test_insufficient_history_returns_flat():
    out = compute_mtf_tiers(_gen_history([100.0] * 10))
    assert out["ready"] is False
    assert out["SHORT"]["ready"] is False


def test_clear_uptrend_all_tiers_up():
    # 250 bars rising 0.3%/day → all three tiers should be UP.
    closes = [100.0 * (1.003 ** i) for i in range(250)]
    out = compute_mtf_tiers(_gen_history(closes))
    assert out["ready"] is True
    assert out["SHORT"]["direction"] == "UP"
    assert out["MEDIUM"]["direction"] == "UP"
    assert out["LONG"]["direction"] == "UP"
    assert out["alignment"]["buy_support"] == 3
    assert out["alignment"]["sell_support"] == 0
    assert out["alignment"]["dominant"] == "UP"
    assert out["alignment"]["all_aligned_up"] is True
    assert out["alignment"]["all_aligned_down"] is False


def test_clear_downtrend_all_tiers_down():
    closes = [200.0 * (0.997 ** i) for i in range(250)]
    out = compute_mtf_tiers(_gen_history(closes))
    assert out["SHORT"]["direction"] == "DOWN"
    assert out["MEDIUM"]["direction"] == "DOWN"
    assert out["LONG"]["direction"] == "DOWN"
    assert out["alignment"]["all_aligned_down"] is True
    assert out["alignment"]["dominant"] == "DOWN"


def test_mixed_trend_recent_reversal():
    """Long uptrend, recent sharp dip → LONG=UP, SHORT=DOWN, dominant MIXED-ish."""
    closes = [100.0 * (1.003 ** i) for i in range(220)]
    # Add a recent dip — last 8 bars drop 5% total
    closes += [closes[-1] * (0.994 ** i) for i in range(1, 12)]
    out = compute_mtf_tiers(_gen_history(closes))
    assert out["LONG"]["direction"] == "UP"
    assert out["SHORT"]["direction"] == "DOWN"
    # Not all aligned in either direction
    assert out["alignment"]["all_aligned_up"] is False
    assert out["alignment"]["all_aligned_down"] is False


# ─────────────── multi_timeframe_gate ───────────────
def test_gate_hold_is_noop():
    closes = [100.0 * (1.003 ** i) for i in range(250)]
    history = _gen_history(closes)
    out = multi_timeframe_gate("HOLD", history, {})
    assert out["aligned"] is True
    assert out["checked"] is False


def test_gate_uses_tiered_mode_when_ready():
    closes = [100.0 * (1.003 ** i) for i in range(250)]
    history = _gen_history(closes)
    out = multi_timeframe_gate("BUY", history, {})
    assert out["mode"] == "tiered"
    assert out["aligned"] is True  # uptrend, BUY allowed
    assert out["htf_trend"] == "UP"


def test_gate_vetoes_counter_trend_buy():
    """BUY into a clear downtrend → all 3 tiers DOWN → veto."""
    closes = [200.0 * (0.997 ** i) for i in range(250)]
    history = _gen_history(closes)
    out = multi_timeframe_gate("BUY", history, {})
    assert out["mode"] == "tiered"
    assert out["aligned"] is False
    assert "counter-trend" in out["reason"].lower()
    assert out["sell_support"] == 3


def test_gate_vetoes_counter_trend_sell():
    closes = [100.0 * (1.003 ** i) for i in range(250)]
    history = _gen_history(closes)
    out = multi_timeframe_gate("SELL", history, {})
    assert out["mode"] == "tiered"
    assert out["aligned"] is False
    assert out["buy_support"] == 3


def test_gate_passes_when_majority_agrees():
    """Mostly uptrend with one tier flat — BUY should still pass (≤1 disagree)."""
    # 250 bars uptrend, then last few bars flat
    closes = [100.0 * (1.003 ** i) for i in range(245)]
    closes += [closes[-1]] * 8
    history = _gen_history(closes)
    out = multi_timeframe_gate("BUY", history, {})
    # SHORT may go FLAT but LONG and MEDIUM still UP — ≥2 UP, no veto.
    assert out["aligned"] is True


def test_gate_passes_explicit_tiers_override():
    """Caller-supplied tiers should be honoured even if history is short."""
    tiers = {
        "SHORT": {"direction": "UP", "ready": True},
        "MEDIUM": {"direction": "UP", "ready": True},
        "LONG": {"direction": "UP", "ready": True},
        "alignment": {"buy_support": 3, "sell_support": 0,
                      "dominant": "UP", "all_aligned_up": True,
                      "all_aligned_down": False},
        "ready": True,
    }
    out = multi_timeframe_gate("BUY", [], {}, mtf_tiers=tiers)
    assert out["mode"] == "tiered"
    assert out["aligned"] is True
    assert out["votes"]["short"] == "UP"


def test_gate_falls_back_to_legacy_when_tiers_not_ready():
    """Empty history → tiers not ready → legacy path returns no-op pass."""
    out = multi_timeframe_gate("BUY", [], {})
    assert out["mode"] == "legacy"
    assert out["aligned"] is True


def test_gate_explicit_tiers_veto():
    """Supplied tiers showing 3-tier downtrend should veto a BUY."""
    tiers = {
        "SHORT": {"direction": "DOWN", "ready": True},
        "MEDIUM": {"direction": "DOWN", "ready": True},
        "LONG": {"direction": "DOWN", "ready": True},
        "alignment": {"buy_support": 0, "sell_support": 3,
                      "dominant": "DOWN", "all_aligned_up": False,
                      "all_aligned_down": True},
        "ready": True,
    }
    out = multi_timeframe_gate("BUY", [], {}, mtf_tiers=tiers)
    assert out["aligned"] is False
    assert "DOWN" in out["votes"]["short"]


# ─────────────── tier indicator content ───────────────
def test_tier_dict_keys_present():
    closes = [100.0 * (1.001 ** i) for i in range(250)]
    out = compute_mtf_tiers(_gen_history(closes))
    for tier in ("SHORT", "MEDIUM", "LONG"):
        d = out[tier]
        assert "sma_fast" in d
        assert "sma_slow" in d
        assert "sma_fast_slope_pct" in d
        assert "rsi" in d
        assert "close_vs_sma_fast_pct" in d
        assert d["ready"] is True


def test_alignment_counts_sum_at_most_three():
    closes = [100.0 + i for i in range(250)]
    out = compute_mtf_tiers(_gen_history(closes))
    a = out["alignment"]
    assert 0 <= a["buy_support"] <= 3
    assert 0 <= a["sell_support"] <= 3
    assert a["buy_support"] + a["sell_support"] <= 3
