"""Tests for the Self-Improving Research Agent (iter-33).

Covers:
  • Trade analyzer — empty history, single-bucket weakness detection
  • Hypothesis generator — validation (drops invalid symbols, clamps fields)
  • Self-improver orchestrator — insufficient data + full run with mocked
                                  hypothesis generator and backtest
  • Routes — accept marks proposal accepted and updates bot config
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
from bson import ObjectId

from research_agent.trade_analyzer import analyze as analyze_trades
from research_agent.hypothesis_generator import _validate, _parse_json
from research_agent.self_improver import run_for_user, _on_cooldown


def _hrs_ago(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


def _fake_trades_db(trades):
    db = MagicMock()
    db.trades.find = MagicMock(return_value=MagicMock(
        limit=MagicMock(return_value=MagicMock(
            to_list=AsyncMock(return_value=trades),
        )),
    ))
    return db


# ============================================================
# Trade analyzer
# ============================================================
@pytest.mark.asyncio
async def test_analyzer_insufficient_history():
    db = _fake_trades_db([])
    out = await analyze_trades(db, user_id="u1", lookback_days=30)
    assert out["trade_count"] == 0
    assert out["weaknesses"] == []
    assert any("more sample" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_analyzer_finds_symbol_weakness():
    # 10 XAU wins, 10 BTC losses → BTC is the weakness
    trades = (
        [{"user_id": "u1", "status": "closed", "symbol": "XAUUSD",
          "pnl": 10, "opened_at": _hrs_ago(i), "closed_at": _hrs_ago(i),
          "action": "BUY"} for i in range(10)]
        + [{"user_id": "u1", "status": "closed", "symbol": "BTCUSD",
            "pnl": -8, "opened_at": _hrs_ago(i + 10), "closed_at": _hrs_ago(i + 10),
            "action": "BUY"} for i in range(10)]
    )
    db = _fake_trades_db(trades)
    out = await analyze_trades(db, user_id="u1", lookback_days=30)
    assert out["trade_count"] == 20
    assert out["overall"]["win_rate"] == 0.5
    weak_buckets = {w["bucket"] for w in out["weaknesses"]}
    assert "BTCUSD" in weak_buckets
    strong_buckets = {s["bucket"] for s in out["strengths"]}
    assert "XAUUSD" in strong_buckets


# ============================================================
# Hypothesis generator validation
# ============================================================
def test_hypothesis_validator_drops_invalid_symbols():
    payload = {"hypotheses": [{
        "name": "Drop FOO", "rationale": "Make focused",
        "compiled": {"symbols": ["FOOBAR"], "session_preference": "london",
                     "risk_level": "low", "strategy_style": "trend_following",
                     "max_concurrent_trades": 2, "auto_execute": True},
    }]}
    out, warns = _validate(payload, ["XAUUSD", "BTCUSD"])
    assert out == []           # FOOBAR not in user symbols → dropped
    assert any("no valid" in w for w in warns)


def test_hypothesis_validator_clamps_fields():
    payload = {"hypotheses": [{
        "name": "x",
        "compiled": {"symbols": ["XAUUSD"], "session_preference": "BOGUS",
                     "risk_level": "EXTREME-NO",
                     "strategy_style": "weird_style",
                     "max_concurrent_trades": 99,
                     "auto_execute": True},
    }]}
    out, _ = _validate(payload, ["XAUUSD"])
    assert len(out) == 1
    c = out[0]["compiled"]
    assert c["session_preference"] == "any"
    assert c["risk_level"] == "medium"
    assert c["strategy_style"] == "trend_following"
    assert c["max_concurrent_trades"] == 5
    # Safety — auto_execute must be False in proposals regardless of input
    assert c["auto_execute"] is False


def test_hypothesis_validator_strips_json_fences():
    text = "```json\n" + '{"hypotheses": []}' + "\n```"
    assert _parse_json(text) == {"hypotheses": []}


# ============================================================
# Self-improver orchestrator
# ============================================================
@pytest.mark.asyncio
async def test_self_improver_insufficient_data(monkeypatch):
    """Few trades → returns skipped_insufficient_data, no LLM call."""
    db = MagicMock()
    db.trades.find = MagicMock(return_value=MagicMock(
        limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[]))),
    ))
    db.research_runs.update_one = AsyncMock()

    monkeypatch.setattr("research_agent.self_improver.generate_hypotheses",
                        AsyncMock(side_effect=AssertionError("should not call LLM")))

    out = await run_for_user(db, "u1", force=True)
    assert out["status"] == "skipped_insufficient_data"
    assert out["proposals"] == []


@pytest.mark.asyncio
async def test_self_improver_full_run(monkeypatch):
    """Healthy trade history → generates hypotheses → backtests → persists."""
    trades = (
        [{"user_id": "u1", "status": "closed", "symbol": "XAUUSD",
          "pnl": 10, "opened_at": _hrs_ago(i), "closed_at": _hrs_ago(i),
          "action": "BUY"} for i in range(12)]
        + [{"user_id": "u1", "status": "closed", "symbol": "BTCUSD",
            "pnl": -5, "opened_at": _hrs_ago(i + 12), "closed_at": _hrs_ago(i + 12),
            "action": "BUY"} for i in range(8)]
    )
    db = _fake_trades_db(trades)
    db.bot_configs.find_one = AsyncMock(return_value={
        "user_id": "u1", "active": True,
        "symbols": ["XAUUSD", "BTCUSD"], "session_preference": "any",
        "risk_level": "medium", "strategy_style": "trend_following",
        "max_concurrent_trades": 2,
    })
    db.research_runs.update_one = AsyncMock()
    # iter-34 added _maybe_auto_accept which reads db.users.find_one.
    # Default to no user/no setting → auto-accept stays disabled in this test.
    db.users.find_one = AsyncMock(return_value=None)
    inserted = []
    async def fake_insert_many(docs):
        inserted.extend(docs)
        return MagicMock(inserted_ids=["x"] * len(docs))
    db.improvement_proposals.insert_many = fake_insert_many

    # Mock LLM hypothesis generator — return 2 fake hypotheses
    monkeypatch.setattr("research_agent.self_improver.generate_hypotheses",
                        AsyncMock(return_value={
                            "hypotheses": [
                                {"name": "Drop BTC", "rationale": "BTC has -P&L",
                                 "compiled": {"symbols": ["XAUUSD"],
                                              "session_preference": "any",
                                              "risk_level": "medium",
                                              "strategy_style": "trend_following",
                                              "max_concurrent_trades": 2,
                                              "auto_execute": False}},
                                {"name": "London only", "rationale": "Sessions vary",
                                 "compiled": {"symbols": ["XAUUSD", "BTCUSD"],
                                              "session_preference": "london",
                                              "risk_level": "low",
                                              "strategy_style": "trend_following",
                                              "max_concurrent_trades": 2,
                                              "auto_execute": False}},
                            ],
                            "notes": [],
                        }))

    # Mock backtest — give "Drop BTC" the higher score
    async def fake_backtest(*, compiled, user_id, lookback_days):  # noqa: ARG001
        if compiled["symbols"] == ["XAUUSD"]:
            return {"win_rate": 0.8, "total_pnl_usd": 100, "matched_trades": 12}
        return {"win_rate": 0.4, "total_pnl_usd": -10, "matched_trades": 8}
    monkeypatch.setattr("research_agent.self_improver.run_backtest", fake_backtest)

    out = await run_for_user(db, "u1", force=True)
    assert out["status"] == "ran"
    assert len(out["proposals"]) == 2
    # Top-ranked is the higher-scoring one
    assert out["proposals"][0]["name"] == "Drop BTC"
    assert out["proposals"][0]["beats_baseline"] is True
    # Two proposals persisted
    assert len(inserted) == 2
    # Cooldown stamp updated
    db.research_runs.update_one.assert_called_once()


@pytest.mark.asyncio
async def test_self_improver_respects_cooldown():
    db = MagicMock()
    db.research_runs.find_one = AsyncMock(return_value={
        "user_id": "u1",
        "last_run_at": _hrs_ago(1),  # 1h ago, cooldown is 24h
    })
    on_cd = await _on_cooldown(db, "u1")
    assert on_cd is True


@pytest.mark.asyncio
async def test_self_improver_cooldown_expired():
    db = MagicMock()
    db.research_runs.find_one = AsyncMock(return_value={
        "user_id": "u1",
        "last_run_at": _hrs_ago(48),  # 2 days ago
    })
    on_cd = await _on_cooldown(db, "u1")
    assert on_cd is False


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
