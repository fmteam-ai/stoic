"""Phase-1 value-driven trading — EV math, quality score, sizing bounds."""
import pytest

from trade_quality import (MIN_EV_USD, QUALITY_GATE_ENABLED, QUALITY_MIN_SCORE,
                           RISK_CAP_PCT, RISK_FLOOR_PCT, WEIGHTS, compute_ev,
                           quality_score, scalp_size_multiplier)

pytestmark = pytest.mark.unit


class TestComputeEV:
    def test_basic_math(self):
        ev = compute_ev(0.6, 10, 5, 2, pip_value_usd_per_lot=10, lot=0.1)
        assert ev["expected_move_pips"] == 4.0      # 0.6*10 - 0.4*5
        assert ev["ev_pips"] == 2.0                 # 4 - 2 cost
        assert ev["ev_usd"] == 2.0                  # 2p * $10 * 0.1
        assert ev["p_win"] == 0.6

    def test_negative_ev(self):
        ev = compute_ev(0.5, 5, 5, 1.5, pip_value_usd_per_lot=10, lot=1.0)
        assert ev["ev_pips"] == -1.5
        assert ev["ev_usd"] == -15.0

    def test_no_lot_no_usd(self):
        ev = compute_ev(0.55, 8, 4, 1)
        assert ev["ev_usd"] is None
        assert ev["ev_pips"] == pytest.approx(1.6, abs=0.01)

    def test_p_clamped(self):
        assert compute_ev(1.5, 10, 5, 0)["p_win"] == 0.99
        assert compute_ev(-1, 10, 5, 0)["p_win"] == 0.01


class TestQualityScore:
    def test_weights_sum_100(self):
        assert sum(WEIGHTS.values()) == 100

    def test_perfect_inputs_near_100(self):
        q = quality_score(ev_pips=5, cost_pips=1, p_win=0.80,
                          trend_alignment=1.0, liquidity=1.0,
                          spread_ratio=0.0, volatility_ratio=1.0, hour_utc=10)
        assert q["score"] == 100

    def test_terrible_inputs_near_0(self):
        q = quality_score(ev_pips=-2, cost_pips=1, p_win=0.50,
                          trend_alignment=0.0, liquidity=0.0,
                          spread_ratio=1.0, volatility_ratio=4.0, hour_utc=3)
        assert q["score"] <= 5

    def test_none_inputs_are_neutral_not_penalty(self):
        q = quality_score(ev_pips=1, cost_pips=1, p_win=0.62)
        neutral = (WEIGHTS["trend_alignment"] + WEIGHTS["liquidity"]
                   + WEIGHTS["spread"] + WEIGHTS["volatility"]
                   + WEIGHTS["time_of_day"]) * 0.5
        assert q["score"] >= neutral

    def test_breakdown_sums_to_score(self):
        q = quality_score(ev_pips=2, cost_pips=1, p_win=0.7,
                          trend_alignment=0.5, liquidity=0.5,
                          spread_ratio=0.5, volatility_ratio=1.5, hour_utc=12)
        assert q["score"] == pytest.approx(sum(q["breakdown"].values()), abs=1)
        assert set(q["breakdown"].keys()) == set(WEIGHTS.keys())

    def test_observe_first_gate_off(self):
        q = quality_score(ev_pips=0, cost_pips=1, p_win=0.5)
        assert q["gate_enabled"] is False
        assert QUALITY_GATE_ENABLED is False
        assert QUALITY_MIN_SCORE == 80


class TestSizing:
    def test_dynamic_bounds_user_decision(self):
        assert RISK_FLOOR_PCT == 0.25
        assert RISK_CAP_PCT == 1.30
        from adaptive_sizing import DEFAULT_CAP_PCT, DEFAULT_FLOOR_PCT
        assert DEFAULT_FLOOR_PCT == 0.25
        assert DEFAULT_CAP_PCT == 1.30

    def test_scalp_multiplier_downscale_only(self):
        assert scalp_size_multiplier(100) == 1.0
        assert scalp_size_multiplier(0) == 0.5
        assert scalp_size_multiplier(50) == 0.75
        assert scalp_size_multiplier(200) == 1.0   # never upscale

    def test_ev_gate_threshold(self):
        assert MIN_EV_USD == 0.0
