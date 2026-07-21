"""Refinements 1/2/5 — execution quality, adaptive min edge, broker stats."""
import pytest

from scalp.exec_quality import (EDGE_CEIL_PIPS, EDGE_FLOOR_PIPS,
                                EQ_WEIGHTS, EXEC_QUALITY_MIN,
                                adaptive_min_edge, execution_quality,
                                session_name)

pytestmark = pytest.mark.unit


class TestExecutionQuality:
    def test_weights_sum_100(self):
        assert sum(EQ_WEIGHTS.values()) == 100

    def test_ideal_conditions_score_100(self):
        eq = execution_quality(spread_pctl=0.0, quote_age_ms=100,
                               avg_slippage_pips=0.05, ack_latency_ms=500,
                               vol_ratio=1.0, hour_utc=9,
                               broker_fill_count=150)
        assert eq["score"] == 100
        assert eq["history_capped"] is False

    def test_hostile_conditions_score_low(self):
        eq = execution_quality(spread_pctl=1.0, quote_age_ms=2500,
                               avg_slippage_pips=0.8, ack_latency_ms=9000,
                               vol_ratio=3.5, hour_utc=22)
        assert eq["score"] <= 5

    def test_none_inputs_neutral_but_history_capped(self):
        eq = execution_quality()
        # neutral half-credit inputs BUT no broker fills → fail-closed cap
        assert eq["score"] <= 39
        assert eq["history_capped"] is True
        assert set(eq["breakdown"].keys()) == set(EQ_WEIGHTS.keys())

    def test_none_inputs_neutral_with_history(self):
        eq = execution_quality(broker_fill_count=150)
        assert 45 <= eq["score"] <= 55
        assert eq["history_capped"] is False

    def test_gate_min_constant(self):
        assert EXEC_QUALITY_MIN == 40
        assert execution_quality()["gate_min"] == 40


class TestAdaptiveMinEdge:
    def test_calm_conditions_stay_at_floor(self):
        ame = adaptive_min_edge(exec_score=95, vol_ratio=1.0,
                                spread_pctl=0.3, loss_streak=0)
        assert ame["min_edge_pips"] == EDGE_FLOOR_PIPS == 0.15

    def test_hostile_conditions_hit_ceiling(self):
        ame = adaptive_min_edge(exec_score=40, vol_ratio=3.0,
                                spread_pctl=0.95, loss_streak=3)
        assert ame["min_edge_pips"] == EDGE_CEIL_PIPS == 0.60

    def test_never_below_floor(self):
        assert adaptive_min_edge()["min_edge_pips"] >= EDGE_FLOOR_PIPS

    def test_component_attribution(self):
        ame = adaptive_min_edge(exec_score=50, vol_ratio=2.0,
                                spread_pctl=0.9, loss_streak=2)
        c = ame["components"]
        assert c["base"] == 0.15
        assert c["execution"] == pytest.approx(0.04, abs=0.001)
        assert c["volatility"] == 0.10
        assert c["spread_regime"] == 0.10
        assert c["loss_streak"] == 0.10


class TestSessions:
    def test_session_mapping(self):
        assert session_name(9) == "london"
        assert session_name(13) == "overlap"
        assert session_name(17) == "newyork"
        assert session_name(22) == "late"
        assert session_name(3) == "asia"


class TestBrokerStats:
    # DB round-trip test moved to tests/integration/scalp/
    # test_iter63_db_roundtrips.py (review item 1 — unit independence)
    @pytest.mark.asyncio
    async def test_empty_record_noop(self):
        from scalp.broker_stats import record
        await record(None, "x")  # no fields → returns before touching db


class TestDecisionProvenance:
    def test_model_version_helper(self):
        from scalp.model import version_of
        assert version_of("no-such-key") is None

    def test_engine_stamps_provenance_and_meta(self):
        import inspect
        from scalp import engine
        src = inspect.getsource(engine)
        assert '"model_version": scalp_model.version_of' in src
        assert '"decision_meta"' in src
        assert "pre_submit_execution_quality" in src
        assert "pre_submit_adaptive_edge" in src
