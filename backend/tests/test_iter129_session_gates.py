"""iter-129 · Session-trend / exhaustion gates + trend-ride + VWAP-fade grind fix.

Replay of the 2026-07-13 session: gold slid 85pts (-2.1%); the bot went
14W/18L (+$67). Every scenario below is a real trade cluster from that day.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from payoff_guard import (session_trend_gate, exhaustion_chase_gate,  # noqa: E402
                          trend_ride_check)
from strategy_engines import hf_scalp_signal  # noqa: E402


def feats(rng, pos, slope, structure="LH_LL"):
    return {"day_range_pct": rng, "range_pos_pct": pos,
            "ema20_slope_pct_2h": slope, "swing_structure": structure}


class TestSessionTrendGate:
    def test_blocks_1057_counter_trend_buy(self):
        # BUY @4059 mid-slide (lost $60.51 across 3 accounts)
        assert session_trend_gate("BUY", feats(0.57, 22.0, -0.06), "XAUUSD")

    def test_blocks_1335_counter_trend_buy(self):
        # BUY @4059 again (lost $49.26 across 3 accounts)
        assert session_trend_gate("BUY", feats(0.71, 38.0, -0.08), "XAUUSD")

    def test_allows_with_trend_sell(self):
        # 12:35 SELL @4062 — with the session trend, won +$37
        assert session_trend_gate("SELL", feats(0.59, 37.0, -0.07), "XAUUSD") is None

    def test_allows_morning_buy_in_up_structure(self):
        # 07:42 BUY @4060 in the early rally, won +$65
        assert session_trend_gate("BUY", feats(0.30, 55.0, 0.05, "HH_HL"), "XAUUSD") is None

    def test_quiet_day_no_gate(self):
        assert session_trend_gate("BUY", feats(0.20, 10.0, -0.10), "XAUUSD") is None

    def test_crypto_threshold_higher(self):
        assert session_trend_gate("BUY", feats(0.66, 12.0, -0.09), "BTCUSD") is None
        assert session_trend_gate("BUY", feats(1.1, 12.0, -0.09), "BTCUSD")

    def test_structure_alone_triggers(self):
        # flat EMA but LH_LL structure still counts as a downtrend
        assert session_trend_gate("BUY", feats(0.60, 20.0, 0.0, "LH_LL"), "XAUUSD")

    def test_hold_ignored(self):
        assert session_trend_gate("HOLD", feats(2.0, 5.0, -0.1), "XAUUSD") is None


class TestExhaustionChaseGate:
    def test_blocks_1752_day_low_sell(self):
        # SELL @3994 at 2% of a 2.1% down-day — all 4 stopped on the bounce (-$159.60)
        assert exhaustion_chase_gate("SELL", feats(2.1, 2.4, -0.10), "XAUUSD")

    def test_blocks_1434_spent_move_sell(self):
        # SELL @4010 at 6% of a 1.74% down-day (-$84.22)
        assert exhaustion_chase_gate("SELL", feats(1.74, 5.6, -0.12), "XAUUSD")

    def test_allows_1405_mid_slide_sell(self):
        # SELL @4037 with only 1.25% elapsed — won +$104, must stay allowed
        assert exhaustion_chase_gate("SELL", feats(1.25, 21.0, -0.11), "XAUUSD") is None

    def test_blocks_buying_spent_up_day(self):
        assert exhaustion_chase_gate("BUY", feats(1.8, 92.0, 0.10, "HH_HL"), "XAUUSD")

    def test_mid_range_entries_allowed_even_on_big_day(self):
        assert exhaustion_chase_gate("SELL", feats(2.0, 40.0, -0.10), "XAUUSD") is None


class TestTrendRide:
    def test_ride_on_big_down_day_with_trend(self):
        r = trend_ride_check("SELL", feats(1.25, 21.0, -0.11), "XAUUSD")
        assert r and r["direction"] == "DOWN"

    def test_no_ride_on_quiet_day(self):
        assert trend_ride_check("SELL", feats(0.6, 21.0, -0.11), "XAUUSD") is None

    def test_no_ride_at_exhaustion_extreme(self):
        # inside the exhaustion band the entry is vetoed anyway — no boost
        assert trend_ride_check("SELL", feats(2.0, 10.0, -0.11), "XAUUSD") is None

    def test_no_ride_against_slope(self):
        assert trend_ride_check("SELL", feats(1.5, 30.0, 0.05), "XAUUSD") is None


class TestVwapFadeGrindFix:
    BTC = {"atr15": 45.0, "trend": "FLAT", "last_price": 62890.0, "ema20": 63050.0,
           "momentum_3h_pct": -0.05, "range_pos_pct": 12.0, "donchian20": "INSIDE",
           "recent_break": None, "day_range_pct": 0.66, "session_vwap": 63271.7}

    def test_blocks_fade_against_down_grind(self):
        # The 109-BUY-signal day: price 0.6% below VWAP but session grinding down
        f = {**self.BTC, "ema20_slope_pct_2h": -0.09, "vwap_dist_pct": -0.597}
        action, reason = hf_scalp_signal(f, fast=True)
        assert action is None and "grinding DOWN" in reason

    def test_blocks_fade_against_up_grind(self):
        f = {**self.BTC, "ema20_slope_pct_2h": 0.09, "vwap_dist_pct": 0.5,
             "range_pos_pct": 50.0}
        action, reason = hf_scalp_signal(f, fast=True)
        assert action is None and "grinding UP" in reason

    def test_allows_fade_when_session_flat(self):
        f = {**self.BTC, "ema20_slope_pct_2h": 0.01, "vwap_dist_pct": -0.4,
             "range_pos_pct": 45.0, "day_range_pct": 0.4}
        action, reason = hf_scalp_signal(f, fast=True)
        assert action == "BUY" and "VWAP fade" in reason

    def test_knife_tightened_to_30pct_band(self):
        # 0.7% day, price at 25% — old 1.0%/20% thresholds let this through
        f = {**self.BTC, "ema20_slope_pct_2h": 0.0, "vwap_dist_pct": -0.4,
             "range_pos_pct": 25.0, "day_range_pct": 0.7}
        action, reason = hf_scalp_signal(f, fast=True)
        assert action is None and "no knife catching" in reason


class TestWiring:
    BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_bot_runner_wires_all_three_gates(self):
        src = open(os.path.join(self.BACKEND, "bot_runner.py")).read()
        for needle in ("session_trend_gate", "exhaustion_chase_gate",
                       "trend_ride_check", "compute_intraday_features",
                       '"session_trend_veto"', '"exhaustion_chase_veto"',
                       '"trend_ride_applied"'):
            assert needle in src, needle

    def test_execution_stamps_scope(self):
        src = open(os.path.join(self.BACKEND, "execution.py")).read()
        assert '"scope": signal.get("scope")' in src
        assert '"trend_ride": signal.get("trend_ride")' in src


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
