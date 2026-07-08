"""Tests for the 5 new specialized agents (iter-29).

Covers:
  • TechnicalAnalysisAgent — trend / RSI / ATR classification
  • MacroAnalysisAgent — XAUUSD-only macro gate consultation + bias digest
  • NewsSentimentAgent — score → label + bias
  • PortfolioAllocatorAgent — vol-parity × Kelly blended trim
  • ExecutionOptimizerAgent — spread / session / slice guards
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


# ============================================================
# TechnicalAnalysisAgent
# ============================================================
@pytest.mark.asyncio
async def test_technical_agent_uptrend_overbought(monkeypatch):
    from agents.technical_agent import TechnicalAnalysisAgent
    fake_indicators = {
        "current_price": 2050.0, "ma_20": 2055.0, "ma_200": 2020.0,
        "rsi_14": 72.0, "atr_14": 20.0,
    }
    monkeypatch.setattr("agents.technical_agent.get_history",
                        AsyncMock(return_value=[{"close": 2000 + i} for i in range(50)]))
    monkeypatch.setattr("agents.technical_agent.compute_indicators",
                        lambda h: fake_indicators)
    monkeypatch.setattr("agents.technical_agent.classify_regime", lambda i: "trending")
    monkeypatch.setattr("agents.technical_agent.current_session",
                        lambda: {"primary": "london", "is_high_volume_window": True})
    monkeypatch.setattr("agents.technical_agent.session_bias_for", lambda s, sess: {"bias": "bullish"})

    agent = TechnicalAnalysisAgent()
    out = await agent.analyze("XAUUSD")
    assert out["trend"] == "UP"
    assert out["rsi_extreme"] == "OVERBOUGHT"
    assert out["regime"] == "trending"
    assert "OVERBOUGHT" in out["bias"]
    # ATR% = 20/2050 * 100 ≈ 0.98
    assert out["atr_pct"] == pytest.approx(0.9756, rel=0.01)


@pytest.mark.asyncio
async def test_technical_agent_flat_trend(monkeypatch):
    from agents.technical_agent import TechnicalAnalysisAgent
    fake_indicators = {
        "current_price": 60000.0, "ma_20": 60100.0, "ma_200": 60050.0,  # tiny spread
        "rsi_14": 50.0, "atr_14": 500.0,
    }
    monkeypatch.setattr("agents.technical_agent.get_history",
                        AsyncMock(return_value=[{"close": 60000} for _ in range(50)]))
    monkeypatch.setattr("agents.technical_agent.compute_indicators",
                        lambda h: fake_indicators)
    monkeypatch.setattr("agents.technical_agent.classify_regime", lambda i: "ranging")
    monkeypatch.setattr("agents.technical_agent.current_session",
                        lambda: {"primary": "ny", "is_high_volume_window": True})
    monkeypatch.setattr("agents.technical_agent.session_bias_for", lambda s, sess: {})

    out = await TechnicalAnalysisAgent().analyze("BTCUSD")
    assert out["trend"] == "FLAT"
    assert out["rsi_extreme"] is None


# ============================================================
# MacroAnalysisAgent — XAUUSD only consults macro_gate
# ============================================================
@pytest.mark.asyncio
async def test_macro_agent_xauusd_consults_gate(monkeypatch):
    from agents.macro_agent import MacroAnalysisAgent
    monkeypatch.setattr("agents.macro_agent.macro_freeze_check",
                        AsyncMock(return_value={"frozen": False}))
    monkeypatch.setattr("agents.macro_agent.upcoming_for",
                        AsyncMock(return_value=[]))
    monkeypatch.setattr("agents.macro_agent.get_gold_positioning",
                        AsyncMock(return_value={"overcrowded_long": False, "overcrowded_short": False}))
    monkeypatch.setattr("agents.macro_agent.get_real_yield",
                        AsyncMock(return_value={"regime": "rising"}))
    monkeypatch.setattr("agents.macro_agent.get_dxy_snapshot",
                        AsyncMock(return_value={"regime": "neutral"}))
    monkeypatch.setattr("agents.macro_agent.get_fred_snapshot",
                        AsyncMock(return_value={"series": []}))
    monkeypatch.setattr("agents.macro_agent.macro_gate_status",
                        AsyncMock(return_value={"buy_ok": False, "sell_ok": True,
                                                "regime": "real_yield_surge",
                                                "blocked": [{"action": "BUY",
                                                             "reason": "10Y +0.30pp"}]}))
    out = await MacroAnalysisAgent().analyze("XAUUSD")
    assert out["macro_gate"]["buy_ok"] is False
    assert "BUY" in out["bias"]


@pytest.mark.asyncio
async def test_macro_agent_btc_skips_gate(monkeypatch):
    from agents.macro_agent import MacroAnalysisAgent
    monkeypatch.setattr("agents.macro_agent.macro_freeze_check",
                        AsyncMock(return_value={"frozen": False}))
    monkeypatch.setattr("agents.macro_agent.upcoming_for",
                        AsyncMock(return_value=[]))
    monkeypatch.setattr("agents.macro_agent.get_fred_snapshot",
                        AsyncMock(return_value={"series": []}))
    out = await MacroAnalysisAgent().analyze("BTCUSD")
    assert out["macro_gate"] is None  # non-gold doesn't consult the gate
    assert out["cot_positioning"] is None
    assert out["dxy"] is None


# ============================================================
# NewsSentimentAgent
# ============================================================
@pytest.mark.asyncio
async def test_news_sentiment_bullish(monkeypatch):
    from agents.news_sentiment_agent import NewsSentimentAgent
    monkeypatch.setattr("agents.news_sentiment_agent.score_sentiment",
                        AsyncMock(return_value={"score": 0.42, "article_count": 18,
                                                "summary": "Fed cut hopes lift gold"}))
    out = await NewsSentimentAgent().analyze("XAUUSD")
    assert out["score"] == pytest.approx(0.42)
    assert out["label"] == "BULLISH"
    assert "BULLISH" in out["bias"]


@pytest.mark.asyncio
async def test_news_sentiment_unknown_on_failure(monkeypatch):
    from agents.news_sentiment_agent import NewsSentimentAgent
    monkeypatch.setattr("agents.news_sentiment_agent.score_sentiment",
                        AsyncMock(side_effect=RuntimeError("api down")))
    out = await NewsSentimentAgent().analyze("BTCUSD")
    assert out["label"] == "UNKNOWN"
    assert out["bias"] == "unavailable"


# ============================================================
# PortfolioAllocatorAgent
# ============================================================
@pytest.mark.asyncio
async def test_allocator_skips_hold(monkeypatch):
    from agents.portfolio_allocator_agent import PortfolioAllocatorAgent
    out = await PortfolioAllocatorAgent().allocate(
        user_id="u1", signal={"action": "HOLD", "lot_size": 0.10, "symbol": "XAUUSD"},
    )
    assert out["applied"] is False
    assert out["adjusted_lot"] == 0.10


@pytest.mark.asyncio
async def test_allocator_kelly_trim_on_losing_streak(monkeypatch):
    from agents.portfolio_allocator_agent import PortfolioAllocatorAgent
    # 1 win / 9 losses → win_rate=0.1 → kelly_scale < 1.0
    fake_trades = (
        [{"pnl": -1.0, "closed_at": datetime.now(timezone.utc).isoformat()}] * 9
        + [{"pnl": 1.0, "closed_at": datetime.now(timezone.utc).isoformat()}]
    )
    fake_db = MagicMock()
    fake_db.trades.find = MagicMock(return_value=MagicMock(
        limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=fake_trades)))
    ))
    monkeypatch.setattr("agents.portfolio_allocator_agent.get_db", lambda: fake_db)

    out = await PortfolioAllocatorAgent().allocate(
        user_id="u1",
        signal={"action": "BUY", "lot_size": 0.10, "symbol": "XAUUSD"},
        technical={"atr_pct": 1.0},
    )
    assert out["applied"] is True
    assert out["adjusted_lot"] < 0.10
    assert out["win_rate"] == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_allocator_neutral_when_no_history(monkeypatch):
    from agents.portfolio_allocator_agent import PortfolioAllocatorAgent
    fake_db = MagicMock()
    fake_db.trades.find = MagicMock(return_value=MagicMock(
        limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[])))
    ))
    monkeypatch.setattr("agents.portfolio_allocator_agent.get_db", lambda: fake_db)

    out = await PortfolioAllocatorAgent().allocate(
        user_id="u1",
        signal={"action": "BUY", "lot_size": 0.05, "symbol": "BTCUSD"},
    )
    # Without history + without active positions, no trim should apply
    assert out["adjusted_lot"] == pytest.approx(0.05)
    assert out["kelly_scale"] == pytest.approx(1.0)


# ============================================================
# ExecutionOptimizerAgent
# ============================================================
@pytest.mark.asyncio
async def test_optimizer_passthrough_on_hold():
    from agents.execution_optimizer_agent import ExecutionOptimizerAgent
    out = await ExecutionOptimizerAgent().optimize(
        signal={"action": "HOLD", "lot_size": 0, "symbol": "XAUUSD"}
    )
    assert out["approved"] is True


@pytest.mark.asyncio
async def test_optimizer_spread_guard_blocks(monkeypatch):
    from agents.execution_optimizer_agent import ExecutionOptimizerAgent
    # Force "active" session by patching _is_xau_off_hours
    monkeypatch.setattr("agents.execution_optimizer_agent._is_xau_off_hours",
                        lambda *a, **kw: False)
    account = {
        "current_spreads": {"XAUUSD": 5.0},   # 5.0 pips observed
        "median_spreads":  {"XAUUSD": 1.0},   # 1.0 pip median → ratio=5.0 > 2.5
    }
    out = await ExecutionOptimizerAgent().optimize(
        signal={"action": "BUY", "lot_size": 0.05, "symbol": "XAUUSD"},
        account=account,
    )
    assert out["approved"] is False
    assert "Spread" in out["deferred_reason"]
    assert out["spread"]["ratio"] == 5.0


@pytest.mark.asyncio
async def test_optimizer_xau_off_hours_defers(monkeypatch):
    from agents.execution_optimizer_agent import ExecutionOptimizerAgent
    monkeypatch.setattr("agents.execution_optimizer_agent._is_xau_off_hours",
                        lambda *a, **kw: True)
    out = await ExecutionOptimizerAgent().optimize(
        signal={"action": "BUY", "lot_size": 0.05, "symbol": "XAUUSD"},
        account=None,
    )
    assert out["approved"] is False
    assert "off-hours" in out["deferred_reason"]
    assert out["session"] == "off_hours"


@pytest.mark.asyncio
async def test_optimizer_slice_plan_for_large_lot(monkeypatch):
    from agents.execution_optimizer_agent import ExecutionOptimizerAgent
    monkeypatch.setattr("agents.execution_optimizer_agent._is_xau_off_hours",
                        lambda *a, **kw: False)
    out = await ExecutionOptimizerAgent().optimize(
        signal={"action": "BUY", "lot_size": 0.20, "symbol": "BTCUSD"},
        account=None,
    )
    assert out["approved"] is True
    assert out["slice_plan"] is not None
    assert sum(out["slice_plan"]) == pytest.approx(0.20)
    assert len(out["slice_plan"]) >= 2
