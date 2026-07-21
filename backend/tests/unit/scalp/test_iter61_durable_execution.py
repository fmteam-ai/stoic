"""Iter-61 — review P0/P1: durable slot link, fail-closed history policy,
broker priors blending, thresholds versioning."""
import inspect

import pytest

from scalp.exec_quality import (FULL_EMPIRICAL_FILLS,
                                INSUFFICIENT_HISTORY_MAX_SCORE,
                                MIN_BROKER_FILLS_FOR_LIVE, THRESHOLDS_VERSION,
                                blend, execution_quality, thresholds_snapshot)

pytestmark = pytest.mark.unit


class TestFailClosedHistoryPolicy:
    def test_perfect_conditions_still_capped_without_fills(self):
        eq = execution_quality(spread_pctl=0.0, quote_age_ms=100,
                               avg_slippage_pips=0.05, ack_latency_ms=500,
                               vol_ratio=1.0, hour_utc=9,
                               broker_fill_count=5)
        assert eq["score"] <= INSUFFICIENT_HISTORY_MAX_SCORE == 39
        assert eq["history_capped"] is True
        assert eq["score"] < eq["gate_min"]        # live blocked

    def test_boundary_at_20_fills(self):
        below = execution_quality(broker_fill_count=19)
        at = execution_quality(spread_pctl=0.0, quote_age_ms=100,
                               avg_slippage_pips=0.05, ack_latency_ms=500,
                               vol_ratio=1.0, hour_utc=9,
                               broker_fill_count=MIN_BROKER_FILLS_FOR_LIVE)
        assert below["history_capped"] is True
        assert at["history_capped"] is False and at["score"] == 100

    def test_constants(self):
        assert MIN_BROKER_FILLS_FOR_LIVE == 20
        assert FULL_EMPIRICAL_FILLS == 100


class TestBrokerPriorBlend:
    def test_no_data(self):
        assert blend(None, None, 0) is None

    def test_prior_only(self):
        assert blend(None, 0.3, 0) == 0.3

    def test_local_only(self):
        assert blend(0.2, None, 5) == 0.2

    def test_weighting_favours_local_as_samples_grow(self):
        assert blend(0.2, 0.4, 0) == pytest.approx(0.4)
        assert blend(0.2, 0.4, 10) == pytest.approx(0.3)
        assert blend(0.2, 0.4, 20) == pytest.approx(0.2)
        assert blend(0.2, 0.4, 50) == pytest.approx(0.2)   # capped at full local


class TestThresholdsVersioning:
    def test_snapshot_and_stamp(self):
        snap = thresholds_snapshot()
        assert snap["version"] == THRESHOLDS_VERSION == 1
        assert snap["exec_quality_min"] == 40
        assert snap["weights"]["spread"] == 22
        eq = execution_quality(broker_fill_count=150)
        assert eq["thresholds_version"] == THRESHOLDS_VERSION


class TestDurableSlotLink:
    def test_slot_link_is_awaited_and_gated(self):
        from scalp import engine
        src = inspect.getsource(engine)
        assert "_link_res = await db.trades.update_one" in src
        assert "_link_res.matched_count == 1" in src
        assert "uncertain_slot_link" in src
        assert "slot_link_failed" in src
        # BrokerSubmitted emit must come AFTER the durable link check
        gate = src.index("_linked = _link_res.matched_count == 1")
        emit = src.index('self._emit(db, "BrokerSubmitted"')
        risk = src.index("self.risk_state.record_open()")
        assert gate < emit < risk

    def test_hot_path_uses_persisted_priors(self):
        from scalp import engine
        src = inspect.getsource(engine)
        assert "_prior = await broker_stats.summary(db, self.broker)" in src
        assert "broker_fill_count=" in src
