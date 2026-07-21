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
                               vol_ratio=1.0, hour_utc=9)
        assert eq["score"] == 100

    def test_hostile_conditions_score_low(self):
        eq = execution_quality(spread_pctl=1.0, quote_age_ms=2500,
                               avg_slippage_pips=0.8, ack_latency_ms=9000,
                               vol_ratio=3.5, hour_utc=22)
        assert eq["score"] <= 5

    def test_none_inputs_neutral(self):
        eq = execution_quality()
        assert 45 <= eq["score"] <= 55
        assert set(eq["breakdown"].keys()) == set(EQ_WEIGHTS.keys())

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
    @pytest.mark.asyncio
    async def test_record_and_summary_roundtrip(self):
        import os
        from dotenv import load_dotenv
        load_dotenv("/app/backend/.env")
        from motor.motor_asyncio import AsyncIOMotorClient
        from scalp.broker_stats import record, summary
        cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = cli[os.environ["DB_NAME"]]
        broker = "TEST_iter60_broker"
        try:
            await record(db, broker, submissions=2, entry_slip_pips=0.2,
                         ack_ms=1200, spread_pips=0.4)
            await record(db, broker, rejects=1, entry_slip_pips=0.4)
            out = await summary(db, broker)
            assert out["totals"]["submissions"] == 2
            assert out["totals"]["rejects"] == 1
            assert out["totals"]["reject_rate"] == pytest.approx(1 / 3, abs=0.01)
            sess = list(out["sessions"].values())[0]
            assert sess["avg_entry_slippage_pips"] == pytest.approx(0.3, abs=0.01)
            assert sess["avg_fill_delay_ms"] == 1200
            assert sess["avg_spread_pips"] == 0.4
        finally:
            await db.scalp_broker_stats.delete_many({"broker_key": broker})
            cli.close()

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
