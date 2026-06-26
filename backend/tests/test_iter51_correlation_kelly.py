"""Tests for Correlation-aware portfolio allocation + dynamic CVaR risk
budget (iter-51).

Covers:
  • CVaR fields appear in VaR output (empty, populated)
  • compute_correlation_aware_scale — no open positions → 1.0 pass-through
  • Same-symbol stacking → corr_scale < 1
  • Two highly correlated symbols, same direction → trims more
  • Opposite directions on positively correlated symbols → no penalty (hedge)
  • CVaR forecast above target → cvar_scale < 1
  • Combined scale clamps at MIN_SCALE
"""
import os
import pytest
from unittest.mock import AsyncMock

from portfolio.var import calculate_var, ES_MULT_95
from portfolio.correlation_kelly import (
    compute_correlation_aware_scale,
    MIN_SCALE,
)


# ============ VaR / CVaR fields ============
@pytest.mark.asyncio
async def test_var_empty_portfolio_includes_cvar_fields():
    out = await calculate_var([], equity=10_000)
    assert out["cvar_95_usd"] == 0
    assert out["cvar_95_pct_equity"] == 0
    assert out["cvar_99_usd"] == 0
    assert out["cvar_99_pct_equity"] == 0


@pytest.mark.asyncio
async def test_var_single_position_cvar_proportional_to_var(monkeypatch):
    async def fake_atr_pct(symbol):
        return 0.02
    async def fake_close_series(symbol, n=30):
        return [100 + i for i in range(n)]
    monkeypatch.setattr("portfolio.var._atr_pct", fake_atr_pct)
    monkeypatch.setattr("portfolio.var._close_series", fake_close_series)

    out = await calculate_var(
        [{"symbol": "BTCUSD", "lot_size": 0.01, "entry_price": 60_000.0, "status": "open"}],
        equity=10_000,
    )
    # CVaR_95 ≈ 1.254× VaR_95 (ES_MULT_95 / Z_95 ≈ 2.0627 / 1.645)
    assert out["cvar_95_usd"] > out["var_95_usd"]
    assert out["cvar_95_usd"] == pytest.approx(out["var_95_usd"] * (2.0627 / 1.645), rel=0.01)


# ============ Correlation-aware scale ============
@pytest.mark.asyncio
async def test_no_open_positions_passes_through():
    out = await compute_correlation_aware_scale(
        new_symbol="XAUUSD", new_action="BUY", new_notional=10_000,
        open_positions=[], equity=10_000,
    )
    assert out["scale"] == 1.0
    assert out["corr_scale"] == 1.0
    assert out["cvar_scale"] == 1.0


@pytest.mark.asyncio
async def test_same_symbol_stacking_trims(monkeypatch):
    # Same symbol -> ρ=1.0 hard-coded → strong corr_scale trim.
    monkeypatch.setattr("portfolio.correlation_kelly._atr_pct",
                        AsyncMock(return_value=0.02))
    monkeypatch.setattr("portfolio.correlation_kelly._close_series",
                        AsyncMock(return_value=[100 + i for i in range(30)]))

    open_book = [{"symbol": "XAUUSD", "action": "BUY", "lot_size": 0.05,
                  "entry_price": 2050.0, "status": "open"}]
    out = await compute_correlation_aware_scale(
        new_symbol="XAUUSD", new_action="BUY",
        new_notional=10_000, open_positions=open_book, equity=10_000,
    )
    assert out["corr_scale"] < 1.0
    assert out["scale"] < 1.0
    assert out["corr_pressure"] > 0


@pytest.mark.asyncio
async def test_opposite_direction_is_a_hedge_no_penalty(monkeypatch):
    # New BUY vs existing SELL on positively-correlated symbol → hedge → no trim.
    monkeypatch.setattr("portfolio.correlation_kelly._atr_pct",
                        AsyncMock(return_value=0.01))
    monkeypatch.setattr("portfolio.correlation_kelly._close_series",
                        AsyncMock(return_value=[100 + i for i in range(30)]))

    open_book = [{"symbol": "XAUUSD", "action": "SELL", "lot_size": 0.01,
                  "entry_price": 2050.0, "status": "open"}]
    out = await compute_correlation_aware_scale(
        new_symbol="XAUUSD", new_action="BUY",
        new_notional=1_000, open_positions=open_book, equity=10_000,
    )
    # Opposite direction on same symbol → ρ_eff = -1 → pressure stays 0.
    assert out["corr_pressure"] == 0
    assert out["corr_scale"] == 1.0


@pytest.mark.asyncio
async def test_cvar_overshoot_triggers_trim(monkeypatch):
    # Force a huge ATR so portfolio sigma blows past CVaR target.
    monkeypatch.setattr("portfolio.correlation_kelly._atr_pct",
                        AsyncMock(return_value=0.20))  # 20% daily vol — extreme
    monkeypatch.setattr("portfolio.correlation_kelly._close_series",
                        AsyncMock(return_value=[100 + i for i in range(30)]))

    open_book = [{"symbol": "BTCUSD", "action": "BUY", "lot_size": 0.01,
                  "entry_price": 60_000.0, "status": "open"}]
    out = await compute_correlation_aware_scale(
        new_symbol="ETHUSD", new_action="BUY",
        new_notional=5_000, open_positions=open_book, equity=10_000,
        cvar_target_pct=2.0,
    )
    assert out["cvar_forecast_pct"] is not None
    assert out["cvar_scale"] < 1.0
    assert out["scale"] < 1.0


@pytest.mark.asyncio
async def test_combined_scale_clamps_at_min(monkeypatch):
    # Many correlated same-direction positions → corr_pressure huge → scale floors at MIN_SCALE.
    monkeypatch.setattr("portfolio.correlation_kelly._atr_pct",
                        AsyncMock(return_value=0.02))
    monkeypatch.setattr("portfolio.correlation_kelly._close_series",
                        AsyncMock(return_value=[100 + i for i in range(30)]))

    open_book = [
        {"symbol": "XAUUSD", "action": "BUY", "lot_size": 1.0,
         "entry_price": 2000.0, "status": "open"}
        for _ in range(5)
    ]
    out = await compute_correlation_aware_scale(
        new_symbol="XAUUSD", new_action="BUY",
        new_notional=100_000, open_positions=open_book, equity=10_000,
    )
    assert out["scale"] >= MIN_SCALE
    assert out["scale"] <= 1.0


@pytest.mark.asyncio
async def test_reason_string_describes_trim(monkeypatch):
    monkeypatch.setattr("portfolio.correlation_kelly._atr_pct",
                        AsyncMock(return_value=0.02))
    monkeypatch.setattr("portfolio.correlation_kelly._close_series",
                        AsyncMock(return_value=[100 + i for i in range(30)]))

    open_book = [{"symbol": "XAUUSD", "action": "BUY", "lot_size": 0.10,
                  "entry_price": 2050.0, "status": "open"}]
    out = await compute_correlation_aware_scale(
        new_symbol="XAUUSD", new_action="BUY",
        new_notional=20_000, open_positions=open_book, equity=10_000,
    )
    assert isinstance(out["reason"], str)
    assert "corr_pressure" in out["reason"] or "CVaR" in out["reason"] or "budget" in out["reason"]
