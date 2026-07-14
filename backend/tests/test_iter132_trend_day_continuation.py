"""iter-132 · Trend-day continuation entries (2026-07-14 CPI 600-pip rally).

The scalp engines had NO entry path on a gap-and-run day: momentum_3h was
diluted to ~0 by the consolidation and price never returned to VWAP. The bot
stood by all day. Two new entry paths fix it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_engines import hf_scalp_signal  # noqa: E402

BASE = {"atr15": 8.0, "trend": "FLAT", "last_price": 4078.0, "ema20": 4076.0,
        "ema20_slope_pct_2h": 0.01, "momentum_3h_pct": 0.02, "vwap_dist_pct": 0.72,
        "range_pos_pct": 75.8, "donchian20": "INSIDE", "recent_break": None,
        "day_range_pct": 3.0, "session_vwap": 4048.0}


class TestTrendDayFlag:
    def test_cpi_day_1422_replay_goes_long(self):
        action, reason = hf_scalp_signal(BASE, fast=True)
        assert action == "BUY" and "continuation long" in reason

    def test_down_day_flag_goes_short(self):
        f = {**BASE, "range_pos_pct": 25.0, "ema20_slope_pct_2h": -0.01,
             "momentum_3h_pct": -0.02, "vwap_dist_pct": -0.6}
        action, reason = hf_scalp_signal(f, fast=True)
        assert action == "SELL" and "continuation short" in reason

    def test_no_flag_on_quiet_day(self):
        f = {**BASE, "day_range_pct": 0.4, "range_pos_pct": 50.0, "vwap_dist_pct": -0.05}
        action, _ = hf_scalp_signal(f, fast=True)
        assert action is None

    def test_no_chase_at_extreme(self):
        f = {**BASE, "range_pos_pct": 92.0}
        action, _ = hf_scalp_signal(f, fast=True)
        assert action is None

    def test_no_flag_against_slope(self):
        f = {**BASE, "ema20_slope_pct_2h": -0.09}
        action, reason = hf_scalp_signal(f, fast=True)
        assert "continuation long" not in (reason or "")

    def test_no_flag_into_fresh_breakdown(self):
        f = {**BASE, "donchian20": "BREAK_DOWN"}
        action, reason = hf_scalp_signal(f, fast=True)
        assert "continuation long" not in (reason or "")


class TestEma20Continuation:
    def test_up_trend_shallow_pullback(self):
        f = {**BASE, "trend": "UP", "last_price": 4076.5, "range_pos_pct": 70.0,
             "ema20_slope_pct_2h": 0.04}
        action, reason = hf_scalp_signal(f, fast=True)
        assert action == "BUY" and "trend-day continuation" in reason

    def test_down_trend_shallow_rally(self):
        f = {**BASE, "trend": "DOWN", "last_price": 4075.5, "ema20": 4076.0,
             "range_pos_pct": 30.0, "ema20_slope_pct_2h": -0.04,
             "momentum_3h_pct": -0.02, "vwap_dist_pct": -0.6}
        action, reason = hf_scalp_signal(f, fast=True)
        assert action == "SELL" and "trend-day continuation" in reason

    def test_pullback_too_deep_no_entry(self):
        f = {**BASE, "trend": "UP", "last_price": 4060.0, "ema20": 4076.0,
             "range_pos_pct": 70.0, "ema20_slope_pct_2h": 0.04,
             "vwap_dist_pct": 0.10}
        action, reason = hf_scalp_signal(f, fast=True)
        # 0.39% below EMA20 — too deep for the continuation path; falls
        # through to the VWAP-bounce check which may still fire on its own.
        assert "trend-day continuation" not in (reason or "")

    def test_normal_day_no_continuation(self):
        f = {**BASE, "trend": "UP", "last_price": 4076.5, "range_pos_pct": 70.0,
             "ema20_slope_pct_2h": 0.04, "day_range_pct": 0.5, "vwap_dist_pct": 0.25}
        action, reason = hf_scalp_signal(f, fast=True)
        assert "trend-day continuation" not in (reason or "")
