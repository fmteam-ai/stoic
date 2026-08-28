"""Tests for the full AI Strategy Generator pipeline (iter-30).

Covers:
  • DSL validator — rejects out-of-vocab fields/ops, clamps param ranges
  • Code generator end-to-end (with mocked Claude response)
  • Optimizer — grid search, baseline-vs-best, qualification floor, notes
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import strategy_code_generator as scg
import strategy_optimizer as so


# ============================================================
# DSL validator
# ============================================================
def _good_dsl():
    return {
        "version": "1.0", "name": "Gold Scalp London",
        "symbols": ["XAUUSD"], "session_preference": "london",
        "params": {"min_confidence": 75, "rsi_oversold": 30,
                   "rsi_overbought": 70, "atr_pct_max": 1.5,
                   "max_concurrent_trades": 2},
        "entry_rules": [
            {"field": "rsi_14", "op": "<", "value_ref": "params.rsi_oversold",
             "side": "BUY", "description": "oversold"}
        ],
        "exit_rules": [
            {"kind": "take_profit_pct", "value": 1.0},
            {"kind": "stop_loss_pct",   "value": 0.5},
        ],
        "pseudocode": "if rsi < 30: buy",
    }


def test_validator_accepts_good_dsl():
    ok, reason = scg._validate(_good_dsl())
    assert ok, reason


def test_validator_rejects_bad_field():
    bad = _good_dsl()
    bad["entry_rules"][0]["field"] = "not_a_real_field"
    ok, reason = scg._validate(bad)
    assert not ok
    assert "field" in reason


def test_validator_rejects_bad_op():
    bad = _good_dsl()
    bad["entry_rules"][0]["op"] = "===="
    ok, reason = scg._validate(bad)
    assert not ok
    assert "op" in reason


def test_validator_requires_tp_and_sl():
    bad = _good_dsl()
    bad["exit_rules"] = [{"kind": "take_profit_pct", "value": 1.0}]  # only 1
    ok, reason = scg._validate(bad)
    assert not ok


def test_validator_clamps_params():
    bad = _good_dsl()
    bad["params"]["min_confidence"] = 999     # out of range
    bad["params"]["rsi_oversold"] = 5         # out of range
    bad["params"]["atr_pct_max"] = 99.0       # out of range
    ok, _ = scg._validate(bad)
    assert ok
    assert bad["params"]["min_confidence"] == 95
    assert bad["params"]["rsi_oversold"] == 20
    assert bad["params"]["atr_pct_max"] == 3.0


# ============================================================
# Code generator end-to-end
# ============================================================
@pytest.mark.asyncio
async def test_code_generator_returns_dsl(monkeypatch):
    fake_llm_response = '{"version":"1.0","name":"x","symbols":["XAUUSD"],"session_preference":"london","params":{"min_confidence":75,"rsi_oversold":30,"rsi_overbought":70,"atr_pct_max":1.5,"max_concurrent_trades":2},"entry_rules":[{"field":"rsi_14","op":"<","value_ref":"params.rsi_oversold","side":"BUY","description":"rsi oversold"}],"exit_rules":[{"kind":"take_profit_pct","value":1.0},{"kind":"stop_loss_pct","value":0.5}],"pseudocode":"if rsi_14 < params.rsi_oversold:\\n    buy(XAUUSD)"}'

    class _FakeChat:
        def __init__(self, *a, **kw): pass
        def with_model(self, *_a): return self
        async def send_message(self, msg): return fake_llm_response

    monkeypatch.setattr("strategy_code_generator.LlmChat", _FakeChat)
    monkeypatch.setenv("EMERGENT_LLM_KEY", "test")

    result = await scg.generate_code({"risk_level": "medium",
                                       "symbols": ["XAUUSD"]})
    assert result.get("error") is None
    assert result["version"] == "1.0"
    assert result["symbols"] == ["XAUUSD"]
    assert "pseudocode" in result
    assert len(result["entry_rules"]) >= 1


@pytest.mark.asyncio
async def test_code_generator_handles_bad_llm_output(monkeypatch):
    class _FakeChat:
        def __init__(self, *a, **kw): pass
        def with_model(self, *_a): return self
        async def send_message(self, msg): return "not even json"

    monkeypatch.setattr("strategy_code_generator.LlmChat", _FakeChat)
    monkeypatch.setenv("EMERGENT_LLM_KEY", "test")

    result = await scg.generate_code({"symbols": ["XAUUSD"]})
    assert result.get("error")


# ============================================================
# Optimizer
# ============================================================
@pytest.mark.asyncio
async def test_optimizer_returns_baseline_when_no_qualified_variants(monkeypatch):
    async def fake_backtest(*, compiled, user_id, lookback_days):  # noqa: ARG001
        # Always 0 trades → no variant qualifies (needs ≥5)
        return {"win_rate": None, "total_pnl_usd": 0.0,
                "matched_trades": 0}
    monkeypatch.setattr("strategy_optimizer.run_backtest", fake_backtest)

    out = await so.optimize(dsl={"symbols": ["XAUUSD"],
                                 "session_preference": "london"},
                            user_id="u1")
    assert out["best"] == out["baseline"]
    assert any("No variant" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_optimizer_finds_better_variant(monkeypatch):
    async def fake_backtest(*, compiled, user_id, lookback_days):  # noqa: ARG001
        # Tokyo session = best by far
        if compiled["session_preference"] == "tokyo":
            return {"win_rate": 0.8, "total_pnl_usd": 200.0,
                    "matched_trades": 20}
        return {"win_rate": 0.4, "total_pnl_usd": 10.0,
                "matched_trades": 10}
    monkeypatch.setattr("strategy_optimizer.run_backtest", fake_backtest)

    out = await so.optimize(dsl={"symbols": ["XAUUSD"],
                                 "session_preference": "any"},
                            user_id="u1")
    assert out["best"]["filters"]["session_preference"] == "tokyo"
    assert out["improvement_pct"] > 0
    assert out["tested_variants"] > 0
    # Transparency note about what's NOT optimized should always be present
    assert any("indicator-threshold" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_optimizer_disqualifies_lucky_2_trade_winner(monkeypatch):
    async def fake_backtest(*, compiled, user_id, lookback_days):  # noqa: ARG001
        # Tokyo: 100% win-rate but only 2 trades → must NOT win
        if compiled["session_preference"] == "tokyo":
            return {"win_rate": 1.0, "total_pnl_usd": 50.0, "matched_trades": 2}
        return {"win_rate": 0.6, "total_pnl_usd": 80.0, "matched_trades": 15}
    monkeypatch.setattr("strategy_optimizer.run_backtest", fake_backtest)

    out = await so.optimize(dsl={"symbols": ["XAUUSD"],
                                 "session_preference": "any"},
                            user_id="u1")
    # The 2-trade variant is below the n=5 floor — must not be declared best.
    assert out["best"]["filters"]["session_preference"] != "tokyo"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
