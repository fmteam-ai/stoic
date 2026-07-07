"""iter-62 · Forecast Agent (Chronos-Bolt) — gate logic + summarize + wiring.
Inference test runs only if chronos is installed (skips otherwise)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forecast_agent import forecast_gate, summarize  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fc(last=100.0, q10=99.0, q50=100.5, q90=102.0):
    return summarize([last], [[q10, q50, q90]], "M15", 8)


class TestForecastGate:
    def test_sell_vetoed_when_whole_band_up(self):
        f = fc(last=100.0, q10=100.4, q50=101.0, q90=102.0)
        r = forecast_gate("SELL", f)
        assert r is not None and "Forecast gate" in r

    def test_buy_vetoed_when_whole_band_down(self):
        f = fc(last=100.0, q10=97.0, q50=98.0, q90=99.5)
        assert forecast_gate("BUY", f) is not None

    def test_mixed_band_allows_both(self):
        f = fc(last=100.0, q10=99.0, q50=100.5, q90=102.0)
        assert forecast_gate("SELL", f) is None
        assert forecast_gate("BUY", f) is None

    def test_none_forecast_fails_open(self):
        assert forecast_gate("SELL", None) is None

    def test_hold_ignored(self):
        assert forecast_gate("HOLD", fc()) is None


class TestSummarize:
    def test_fields(self):
        f = fc(last=100.0, q10=99.0, q50=100.5, q90=102.0)
        assert f["last"] == 100.0 and f["q50"] == 100.5
        assert f["median_change_pct"] == pytest.approx(0.5)
        assert f["band_low_pct"] == pytest.approx(-1.0)
        assert f["band_high_pct"] == pytest.approx(2.0)


@pytest.mark.skipif(
    not os.environ.get("RUN_CHRONOS_TEST"),
    reason="set RUN_CHRONOS_TEST=1 to run real model inference")
def test_real_inference():
    from forecast_agent import _forecast_sync
    closes = [100 + i * 0.1 for i in range(100)]
    q = _forecast_sync(closes, 8)
    assert q is not None and len(q) == 8 and len(q[0]) == 3
    # rising series → median forecast should be near/above last close
    assert q[-1][1] > 105.0


class TestWiring:
    def test_bot_runner_forecast_gate(self):
        src = open(os.path.join(BACKEND, "bot_runner.py")).read()
        assert "forecast_gate" in src and '"forecast_gate_advice"' in src
        assert '"forecast_gate_block"' in src

    def test_config_model_has_mode(self):
        src = open(os.path.join(BACKEND, "models.py")).read()
        assert 'forecast_gate_mode: str = "advisory"' in src
