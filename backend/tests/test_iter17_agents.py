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
def _corr_history(n=25):
    """iter-142: candles with dates + proportional daily returns so the
    returns-based correlation reads ~1.0 between the two series."""
    import math as _m
    rets = [0.01 * _m.sin(i) for i in range(n)]
    xau, btc, px_a, px_b = [], [], 2000.0, 60000.0
    for i, r in enumerate(rets):
        px_a *= (1 + r)
        px_b *= (1 + r * 2)     # scaled but perfectly correlated returns
        d = f"2026-05-{(i % 28) + 1:02d}" if i < 28 else f"2026-06-{i - 27:02d}"
        xau.append({"date": d, "close": px_a})
        btc.append({"date": d, "close": px_b})
    return xau, btc


def test_cross_asset_veto_fires_on_same_direction_correlated():
    from agents.risk_agent import RiskAgent
    agent = RiskAgent(corr_threshold=0.7, lookback_bars=20)
    xau, btc = _corr_history()

    async def fake_history(sym):
        return xau if sym == "XAUUSD" else btc

    open_positions = [
        {"symbol": "BTCUSD", "action": "BUY", "status": "open"},
    ]
    with patch("portfolio.var.get_history", side_effect=fake_history):
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
    xau, btc = _corr_history()

    async def fake_history(sym):
        return xau if sym == "XAUUSD" else btc

    signal = {
        "symbol": "XAUUSD",
        "action": "BUY",
        "tradeable": True,
        "reasoning": "Claude says buy.",
    }
    positions = [{"symbol": "BTCUSD", "action": "BUY", "status": "open"}]
    with patch("portfolio.var.get_history", side_effect=fake_history):
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
    # Allocator's win-rate query needs a trades cursor
    fake_db.trades.find = MagicMock(return_value=MagicMock(
        limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[]))),
    ))

    fake_signal = {
        "action": "BUY", "confidence": 78.0, "tradeable": True,
        "symbol": "XAUUSD", "lot_size": 0.05, "reasoning": "ok",
    }

    with patch("agents.orchestrator.get_db", return_value=fake_db), \
         patch("agents.portfolio_allocator_agent.get_db", return_value=fake_db), \
         patch.object(orch.technical, "analyze",
                      AsyncMock(return_value={"symbol": "XAUUSD", "bias": "trend=UP",
                                              "atr_pct": 1.0, "indicators": {}})), \
         patch.object(orch.macro, "analyze",
                      AsyncMock(return_value={"symbol": "XAUUSD", "bias": "neutral",
                                              "macro_freeze": {"frozen": False},
                                              "upcoming_events": [], "fred": None,
                                              "real_yield_10y": None, "dxy": None,
                                              "cot_positioning": None,
                                              "macro_gate": None})), \
         patch.object(orch.news, "analyze",
                      AsyncMock(return_value={"symbol": "XAUUSD", "score": 0.1,
                                              "label": "NEUTRAL", "article_count": 5,
                                              "summary": "", "bias": "NEUTRAL"})), \
         patch.object(orch.strategy, "propose",
                      AsyncMock(return_value=fake_signal)), \
         patch.object(orch.risk, "review",
                      AsyncMock(return_value={
                          "approved": True, "signal": fake_signal, "overrides": [],
                      })), \
         patch("agents.execution_optimizer_agent._is_xau_off_hours",
               lambda *a, **kw: False):
        out = _arun(orch.analyze_tick(
            user_id="u1", symbol="XAUUSD", risk_level="medium",
            active_positions=[],
        ))

    assert out["signal"] is fake_signal
    assert out["tick_id"]
    assert len(inserted) == 1
    steps = inserted[0]["steps"]
    agent_names = [s["agent"] for s in steps]
    # New 7-agent pipeline: 3 parallel analysers → strategy → risk → allocator → exec_optimizer
    assert "technical" in agent_names
    assert "macro" in agent_names
    assert "news_sentiment" in agent_names
    assert "strategy" in agent_names
    assert "risk" in agent_names
    assert "portfolio_allocator" in agent_names
    assert "execution_optimizer" in agent_names
    assert inserted[0]["final_action"] == "BUY"
    assert inserted[0]["final_confidence"] == 78.0
