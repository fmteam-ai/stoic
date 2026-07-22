"""Phase E — Market Data Layer: tick validation, gap detection, session
quality metrics, feed-health integration. MongoDB-free."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import inspect

import pytest

from scalp import market_data as md

pytestmark = pytest.mark.unit


class TestTickValidation:
    def test_valid_tick(self):
        v = md.validate_tick(1.0850, 1.0851)
        assert v["ok"] is True and v["suspect"] is False

    def test_non_positive_rejected(self):
        assert md.validate_tick(0, 1.0851)["reason"] == "non_positive"
        assert md.validate_tick(1.0850, -1)["reason"] == "non_positive"

    def test_inverted_rejected(self):
        v = md.validate_tick(1.0855, 1.0850)
        assert v["ok"] is False and v["reason"] == "inverted"

    def test_absurd_spread_rejected(self):
        v = md.validate_tick(1.0000, 1.0600)          # 6% spread
        assert v["ok"] is False and v["reason"] == "absurd_spread"

    def test_price_jump_flagged_not_dropped(self):
        v = md.validate_tick(1.1100, 1.1101, last_mid=1.0850)   # +2.3%
        assert v["ok"] is True                        # flash moves kept
        assert v["suspect"] is True and v["reason"] == "price_jump"

    def test_unparseable_rejected(self):
        assert md.validate_tick("x", 1.0851)["reason"] == "unparseable"


class TestGapDetection:
    def test_gap_events_counted(self):
        m = md.DataQualityMonitor()
        t = 1_000_000
        m.record_tick(t, 0.8)
        m.record_tick(t + 500, 0.8)                   # normal
        m.record_tick(t + 500 + 15_000, 0.8)          # 15s gap → alert
        assert m.gap_events == 1
        assert m.max_gap_ms == 15_000

    def test_session_break_not_a_gap(self):
        m = md.DataQualityMonitor()
        m.record_tick(1_000_000, 0.8)
        m.record_tick(1_000_000 + 2 * md.SESSION_BREAK_MS, 0.8)  # weekend
        assert m.gap_events == 0
        assert len(m.gaps) == 0


class TestSessionQuality:
    def _clean(self, n=200):
        m = md.DataQualityMonitor()
        for i in range(n):
            m.record_tick(1_000_000 + i * 400, 0.8)
        return m

    def test_clean_session_is_good(self):
        snap = self._clean().snapshot()
        assert snap["rating"] == md.GOOD
        assert snap["score"] >= 95
        assert snap["median_spread_pips"] == 0.8

    def test_invalid_ticks_degrade(self):
        m = self._clean()
        for _ in range(25):                           # ~11% invalid
            m.record_invalid("inverted")
        snap = m.snapshot()
        assert snap["score"] < 70
        assert snap["ticks_invalid"] == 25

    def test_spread_anomalies_tracked(self):
        m = self._clean()
        for i in range(30):                           # 3× median spread burst
            m.record_tick(1_200_000 + i * 400, 3.5)
        snap = m.snapshot()
        assert snap["spread_anomaly_ratio"] > 0.1
        assert snap["p95_spread_pips"] >= 3.0

    def test_poor_rating_on_compound_failures(self):
        m = md.DataQualityMonitor()
        t = 1_000_000
        for i in range(50):
            m.record_tick(t + i * 12_000, 0.8)        # every gap is an alert
        for _ in range(10):
            m.record_invalid("non_positive")
        for _ in range(10):
            m.record_out_of_order()
        assert m.snapshot()["rating"] == md.POOR


class TestPhaseEWiring:
    def _src(self):
        from scalp import engine
        return inspect.getsource(engine)

    def test_engine_validates_before_state(self):
        src = self._src()
        assert "market_data.validate_tick(" in src
        assert "record_invalid(" in src
        assert "record_out_of_order(" in src
        assert "record_tick(" in src
        # validation must run BEFORE the state update
        assert src.index("market_data.validate_tick(") < \
            src.index("self.state.update(t, trusted=trusted)")

    def test_quality_feeds_kill_switch(self):
        ksrc = open(_os.path.join(_BACKEND_DIR, "scalp/kill.py")).read()
        assert "data_quality" in ksrc
        assert "market data quality poor" in ksrc
        src = self._src()
        assert "kill.evaluate(self.state, self.cfg" in src
        assert "data_quality" in src

    def test_status_exposes_data_quality(self):
        src = self._src()
        assert '"data_quality"' in src


class TestKillIntegration:
    def test_poor_quality_halts_entries(self):
        from scalp import kill

        class _S:
            last_tick = None
            clock_drift_ms = 0
            spreads = []
            fills_seen = 0
            slippage_ewma_pips = 0.0

            def quote_age_ms(self):
                return 0

            def reject_rate(self):
                return 0.0

            def spread_pips(self):
                return None

        class _C:
            max_quote_age_ms = 900
            max_spread_pips = 1.2
            max_expected_slippage_pips = 0.5

        out = kill.evaluate(_S(), _C(), data_quality={"rating": "POOR",
                                                      "score": 40})
        assert out["status"] == "HALTED"
        assert any("quality" in r for r in out["reasons"])
        assert out["close_allowed"] is True           # reducing risk always

    def test_good_quality_adds_no_reason(self):
        from scalp import kill

        class _S:
            last_tick = None
            clock_drift_ms = 0
            spreads = []
            fills_seen = 0
            slippage_ewma_pips = 0.0

            def quote_age_ms(self):
                return 0

            def reject_rate(self):
                return 0.0

            def spread_pips(self):
                return None

        class _C:
            max_quote_age_ms = 900
            max_spread_pips = 1.2
            max_expected_slippage_pips = 0.5

        out = kill.evaluate(_S(), _C(), data_quality={"rating": "GOOD",
                                                      "score": 98})
        assert not any("quality" in r for r in out["reasons"])
