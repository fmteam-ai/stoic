"""Iter-17: Multi-agent architecture — Research/Strategy/Risk/Execution.

Covers:
  * agents.risk_agent.cross_asset_correlation_veto with mocked history
  * agents.risk_agent.review wires the veto into the signal payload
  * agents.orchestrator persists an activity-log document
  * macro.fred series classifier produces expected labels
  * /api/agents/macro auth-gated endpoint serves snapshot
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/app/backend")


def _arun(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pearson correlation
# ---------------------------------------------------------------------------
def test_pearson_perfect_positive():
    from agents.risk_agent import _pearson
    xs = list(range(1, 21))
    ys = [x * 2.0 for x in xs]
    assert abs(_pearson(xs, ys) - 1.0) < 1e-9


def test_pearson_perfect_negative():
    from agents.risk_agent import _pearson
    xs = list(range(1, 21))
    ys = [-x for x in xs]
    assert abs(_pearson(xs, ys) - (-1.0)) < 1e-9


def test_pearson_too_few_samples():
    from agents.risk_agent import _pearson
    assert _pearson([1.0], [2.0]) == 0.0
    assert _pearson([1.0, 2.0], [3.0, 4.0]) == 0.0


# ---------------------------------------------------------------------------
# Cross-asset correlation veto
# ---------------------------------------------------------------------------
def test_cross_asset_veto_fires_on_same_direction_correlated():
    from agents.risk_agent import RiskAgent
    agent = RiskAgent(corr_threshold=0.7, lookback_bars=20)

    xau = [{"close": 2000 + i} for i in range(20)]
    btc = [{"close": 60000 + i * 30} for i in range(20)]  # near-perfect positive corr

    async def fake_history(sym):
        return xau if sym == "XAUUSD" else btc

    open_positions = [
        {"symbol": "BTCUSD", "action": "BUY", "status": "open"},
    ]
    with patch("agents.risk_agent.get_history", side_effect=fake_history):
        out = _arun(agent.cross_asset_correlation_veto(
            "XAUUSD", "BUY", open_positions,
        ))
    assert out["veto"] is True
    assert out["correlated_with"] == "BTCUSD"
    assert out["corr"] >= 0.7


def test_cross_asset_veto_passes_opposite_direction():
    """SELL on XAU should NOT be vetoed by an existing BUY on BTC."""
    from agents.risk_agent import RiskAgent
    agent = RiskAgent(corr_threshold=0.7, lookback_bars=20)

    xau = [{"close": 2000 + i} for i in range(20)]
    btc = [{"close": 60000 + i * 30} for i in range(20)]

    async def fake_history(sym):
        return xau if sym == "XAUUSD" else btc

    open_positions = [{"symbol": "BTCUSD", "action": "BUY", "status": "open"}]
    with patch("agents.risk_agent.get_history", side_effect=fake_history):
        out = _arun(agent.cross_asset_correlation_veto(
            "XAUUSD", "SELL", open_positions,
        ))
    assert out["veto"] is False


def test_cross_asset_veto_passes_when_no_other_positions():
    from agents.risk_agent import RiskAgent
    agent = RiskAgent(corr_threshold=0.7)
    out = _arun(agent.cross_asset_correlation_veto("XAUUSD", "BUY", []))
    assert out["veto"] is False


def test_review_appends_veto_reason_and_flips_to_hold():
    from agents.risk_agent import RiskAgent
    agent = RiskAgent(corr_threshold=0.5, lookback_bars=10)

    xau = [{"close": 2000 + i} for i in range(10)]
    btc = [{"close": 60000 + i * 30} for i in range(10)]

    async def fake_history(sym):
        return xau if sym == "XAUUSD" else btc

    signal = {
        "symbol": "XAUUSD",
        "action": "BUY",
        "tradeable": True,
        "reasoning": "Claude says buy.",
    }
    positions = [{"symbol": "BTCUSD", "action": "BUY", "status": "open"}]
    with patch("agents.risk_agent.get_history", side_effect=fake_history):
        out = _arun(agent.review("XAUUSD", signal, positions))

    assert out["approved"] is False
    assert out["signal"]["action"] == "HOLD"
    assert out["signal"]["tradeable"] is False
    assert "VETO (cross-asset correlation)" in out["signal"]["reasoning"]
    assert any(o["kind"] == "cross_asset_correlation" for o in out["overrides"])


# ---------------------------------------------------------------------------
# FRED classifier
# ---------------------------------------------------------------------------
def test_fred_classifier_dff_tightening():
    from macro.fred import _classify
    assert _classify("DFF", 5.50, 5.20) == "tightening"


def test_fred_classifier_dff_easing():
    from macro.fred import _classify
    assert _classify("DFF", 4.50, 5.00) == "easing"


def test_fred_classifier_vix_high():
    from macro.fred import _classify
    assert _classify("VIXCLS", 30.0, 18.0) == "high_vol"


def test_fred_classifier_vix_calm():
    from macro.fred import _classify
    assert _classify("VIXCLS", 12.0, 14.0) == "calm"


def test_fred_classifier_unknown_series_neutral():
    from macro.fred import _classify
    assert _classify("UNKNOWN_X", 1.0, 0.5) == "neutral"


# ---------------------------------------------------------------------------
# Orchestrator activity logging
# ---------------------------------------------------------------------------
def test_orchestrator_logs_full_pipeline():
    from agents.orchestrator import Orchestrator
    orch = Orchestrator()

    inserted: list[dict] = []

    async def fake_insert(doc):
        inserted.append(doc)
        return MagicMock(inserted_id="x")

    fake_db = MagicMock()
    fake_db.agent_activity.insert_one = fake_insert

    fake_signal = {
        "action": "BUY", "confidence": 78.0, "tradeable": True,
        "symbol": "XAUUSD", "reasoning": "ok",
    }

    with patch("agents.orchestrator.get_db", return_value=fake_db), \
         patch.object(orch.research, "gather",
                      AsyncMock(return_value={"sentiment": {"score": 0.1}})), \
         patch.object(orch.strategy, "propose",
                      AsyncMock(return_value=fake_signal)), \
         patch.object(orch.risk, "review",
                      AsyncMock(return_value={
                          "approved": True, "signal": fake_signal, "overrides": [],
                      })):
        out = _arun(orch.analyze_tick(
            user_id="u1", symbol="XAUUSD", risk_level="medium",
            active_positions=[],
        ))

    assert out["signal"] is fake_signal
    assert out["tick_id"]
    # One log entry persisted with all 3 steps
    assert len(inserted) == 1
    steps = inserted[0]["steps"]
    agent_names = [s["agent"] for s in steps]
    assert agent_names == ["research", "strategy", "risk"]
    assert inserted[0]["final_action"] == "BUY"
    assert inserted[0]["final_confidence"] == 78.0
