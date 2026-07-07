"""iter-64 · Probabilistic forecasting — CDF interpolation, scenario table,
probability-weighted trade evaluation."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prob_forecast import cdf_at, scenario_table, trade_eval  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LEVELS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
# XAUUSD pip = 0.1 → +1.0 price = +10 pips. Bullish-skewed distribution.
BULL_VALS = [4098.0, 4100.0, 4101.5, 4103.0, 4104.5, 4106.0, 4107.5, 4109.0, 4111.0]
LAST = 4100.0


def fc(values=BULL_VALS, last=LAST):
    return {"last": last, "quantile_levels": LEVELS, "quantile_values": values,
            "q10": values[0], "q50": values[4], "q90": values[-1]}


class TestCdf:
    def test_median(self):
        assert cdf_at(4104.5, LEVELS, BULL_VALS) == pytest.approx(0.5)

    def test_monotone(self):
        pts = [cdf_at(x, LEVELS, BULL_VALS) for x in (4095, 4100, 4104.5, 4110, 4115)]
        assert pts == sorted(pts)
        assert 0.0 <= pts[0] < pts[-1] <= 1.0

    def test_far_tails(self):
        assert cdf_at(4050.0, LEVELS, BULL_VALS) == 0.0
        assert cdf_at(4150.0, LEVELS, BULL_VALS) == 1.0


class TestScenarioTable:
    def test_bullish_distribution(self):
        d = scenario_table("XAUUSD", LAST, LEVELS, BULL_VALS)
        assert d["p_up"] > 0.7                      # last sits at q20
        assert d["ev_pips_long"] > 0
        labels = {s["label"] for s in d["scenarios"]}
        assert labels == {"upside", "mild_adverse", "severe_tail"}
        assert sum(s["prob"] for s in d["scenarios"]) == pytest.approx(1.0, abs=0.02)

    def test_scenario_signs(self):
        d = scenario_table("XAUUSD", LAST, LEVELS, BULL_VALS)
        by = {s["label"]: s for s in d["scenarios"]}
        assert by["upside"]["pips"] > 0
        assert by["severe_tail"]["pips"] < by["mild_adverse"]["pips"] <= 0


class TestTradeEval:
    def test_buy_positive_ev(self):
        pe = trade_eval("XAUUSD", "BUY", LAST, 4095.0, 4106.0, fc())
        assert pe["ev_pips"] > 0 and not pe["negative_ev"]
        assert pe["lot_multiplier"] == 1.0
        assert 0 < pe["p_tp"] < 1 and 0 <= pe["p_sl"] < 0.2

    def test_sell_negative_ev_flagged(self):
        pe = trade_eval("XAUUSD", "SELL", LAST, 4105.0, 4094.0, fc())
        assert pe["ev_pips"] < 0 and pe["negative_ev"]
        assert pe["lot_multiplier"] == 0.5

    def test_suggested_sl_only_when_tighter(self):
        # BUY with very wide SL → q10 (4098) is tighter → suggested
        pe = trade_eval("XAUUSD", "BUY", LAST, 4085.0, 4106.0, fc())
        assert pe["suggested_sl"] == pytest.approx(4098.0)
        # BUY with SL already inside q10 → no suggestion
        pe2 = trade_eval("XAUUSD", "BUY", LAST, 4099.0, 4106.0, fc())
        assert pe2["suggested_sl"] is None

    def test_fails_open(self):
        assert trade_eval("XAUUSD", "BUY", LAST, 4095.0, 4106.0, None) is None
        assert trade_eval("XAUUSD", "BUY", None, 4095.0, 4106.0, fc()) is None
        assert trade_eval("XAUUSD", "HOLD", LAST, 4095.0, 4106.0, fc()) is None


class TestWiring:
    def test_bot_runner_prob_layer(self):
        src = open(os.path.join(BACKEND, "bot_runner.py")).read()
        for tok in ("trade_eval", '"prob_ev_negative"', '"prob_ev_block"',
                    "prob_lot_scale"):
            assert tok in src, tok

    def test_config_mode(self):
        assert 'prob_forecast_mode: str = "advisory"' in \
            open(os.path.join(BACKEND, "models.py")).read()

    def test_forecast_payload_has_distribution(self):
        src = open(os.path.join(BACKEND, "forecast_agent.py")).read()
        assert "QUANTILE_LEVELS" in src and "scenario_table" in src
