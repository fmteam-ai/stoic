"""Tests for iter-34: auto-accept opt-in + Explainable AI trade snapshots."""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from bson import ObjectId

from research_agent.self_improver import _maybe_auto_accept
from trade_explainer import (
    explain_trade, _explain_entry, _explain_factors, _explain_risks,
    _explain_sizing,
)


# ============================================================
# Auto-accept opt-in
# ============================================================
@pytest.mark.asyncio
async def test_auto_accept_disabled_by_default():
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value={"_id": "u1"})  # no setting
    applied = await _maybe_auto_accept(
        db, user_id="u1",
        proposals=[{"name": "x", "beats_baseline": True,
                    "delta_vs_baseline": 0.5,
                    "compiled": {"symbols": ["XAUUSD"]}}],
        inserted_ids=[str(ObjectId())],
    )
    assert applied is False


@pytest.mark.asyncio
async def test_auto_accept_below_threshold_skipped():
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value={
        "_id": "u1",
        "research_auto_accept": {"enabled": True, "min_delta_pct": 10.0},
    })
    applied = await _maybe_auto_accept(
        db, user_id="u1",
        proposals=[{"name": "tiny", "beats_baseline": True,
                    "delta_vs_baseline": 0.05,  # 5% < 10% threshold
                    "compiled": {"symbols": ["XAUUSD"]}}],
        inserted_ids=[str(ObjectId())],
    )
    assert applied is False


@pytest.mark.asyncio
async def test_auto_accept_applies_when_threshold_cleared():
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value={
        "_id": "u1",
        "research_auto_accept": {"enabled": True, "min_delta_pct": 10.0},
    })
    # iter-36: auto-accept now uses the shared targeting helper, which calls
    # bot_configs.find(...).to_list() and accounts.find(...).to_list().
    bot_cfg_doc = {"_id": ObjectId(), "user_id": "u1", "account_id": None,
                   "symbols": ["XAUUSD"], "risk_level": "middle"}
    _bc_cursor = MagicMock()
    _bc_cursor.to_list = AsyncMock(return_value=[bot_cfg_doc])
    db.bot_configs.find = MagicMock(return_value=_bc_cursor)
    _acc_cursor = MagicMock()
    _acc_cursor.to_list = AsyncMock(return_value=[])
    db.accounts.find = MagicMock(return_value=_acc_cursor)
    db.bot_configs.update_one = AsyncMock()
    db.improvement_proposals.update_one = AsyncMock()
    db.improvement_proposals.update_many = AsyncMock()

    top_id = str(ObjectId())
    applied = await _maybe_auto_accept(
        db, user_id="u1",
        proposals=[{
            "name": "Better",
            "beats_baseline": True,
            "delta_vs_baseline": 0.25,  # 25% — well above 10%
            "compiled": {"symbols": ["XAUUSD"], "session_preference": "london",
                         "risk_level": "low", "strategy_style": "trend_following",
                         "max_concurrent_trades": 2},
        }],
        inserted_ids=[top_id],
    )
    assert applied is True
    # bot_configs updated (now via the helper, still update_one per touched doc)
    db.bot_configs.update_one.assert_called_once()
    # accepted proposal marked
    db.improvement_proposals.update_one.assert_called_once()


@pytest.mark.asyncio
async def test_auto_accept_skips_when_doesnt_beat_baseline():
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value={
        "_id": "u1",
        "research_auto_accept": {"enabled": True, "min_delta_pct": 5.0},
    })
    applied = await _maybe_auto_accept(
        db, user_id="u1",
        proposals=[{"name": "x", "beats_baseline": False,
                    "delta_vs_baseline": 0.5,
                    "compiled": {"symbols": ["XAUUSD"]}}],
        inserted_ids=[str(ObjectId())],
    )
    assert applied is False


# ============================================================
# Trade Explainer
# ============================================================
def test_explain_entry_composes_from_steps():
    activity = {
        "final_action": "BUY", "final_confidence": 78,
        "steps": [
            {"agent": "strategy",       "status": "ok", "summary": "BUY 78%"},
            {"agent": "technical",      "status": "ok", "summary": "trend=UP · RSI=42"},
            {"agent": "macro",          "status": "ok", "summary": "neutral"},
            {"agent": "news_sentiment", "status": "ok", "summary": "BULLISH (+0.35)"},
        ],
    }
    out = _explain_entry(activity, {"action": "BUY", "confidence": 78})
    assert out["action"] == "BUY"
    assert out["confidence"] == 78
    assert out["strategy_summary"] == "BUY 78%"
    assert "BULLISH" in out["news_bias"]


def test_explain_factors_ranks_top_3():
    activity = {"steps": [
        {"agent": "strategy", "status": "ok", "summary": "BUY 82%"},
        {"agent": "macro",    "status": "ok", "summary": "FREEZE imminent NFP"},
        {"agent": "news_sentiment", "status": "ok", "summary": "BEARISH (-0.4)"},
        {"agent": "technical", "status": "ok", "summary": "OVERSOLD"},
        {"agent": "risk", "status": "ok", "summary": "approved"},
    ]}
    factors = _explain_factors(activity)
    assert len(factors) == 3   # capped at 3
    labels = {f["label"] for f in factors}
    assert "Strategy Confidence" in labels
    assert "Macro Regime" in labels


def test_explain_sizing_pulls_allocator_details():
    activity = {"steps": [
        {"agent": "portfolio_allocator", "status": "ok",
         "summary": "lot 0.10→0.07",
         "details": {"original_lot": 0.10, "vol_parity_scale": 0.85,
                     "kelly_scale": 0.82, "blended_scale": 0.7,
                     "win_rate": 0.45, "atr_pct": 1.2,
                     "reason": "Kelly trim ×0.82"}},
    ]}
    trade = {"lot_size": 0.07}
    out = _explain_sizing(activity, trade)
    assert out["final_lot"] == 0.07
    assert out["original_lot"] == 0.10
    assert out["blended_scale"] == 0.7
    assert out["applied"] is True
    assert "Kelly" in out["reason"]


def test_explain_risks_includes_safety_audit():
    trade = {
        "stop_loss": 2040, "take_profit": 2070,
        "safety_audit": {"sl_distance_pips": 100,
                         "max_loss_usd": 50.0,
                         "risk_pct_of_equity": 0.5},
    }
    out = _explain_risks(trade, {})
    assert out["stop_loss"] == 2040
    assert out["max_loss_usd"] == 50.0
    assert out["risk_pct_of_equity"] == 0.5


@pytest.mark.asyncio
async def test_explain_trade_prefers_snapshot():
    trade = {
        "_id": ObjectId(), "symbol": "XAUUSD", "user_id": "u1",
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "explanation_snapshot": {
            "entry": {"action": "BUY"}, "sizing": {"final_lot": 0.05},
            "factors": [], "risks": {},
            "snapshotted_at": "frozen",
        },
    }
    db = MagicMock()
    # If snapshot is preferred, agent_activity.find_one is never called
    db.agent_activity.find_one = AsyncMock(side_effect=AssertionError("should not call"))
    out = await explain_trade(db, trade)
    assert out["entry"]["action"] == "BUY"
    assert out["snapshotted_at"] == "frozen"


@pytest.mark.asyncio
async def test_explain_trade_falls_back_to_live_composition():
    trade = {
        "_id": ObjectId(), "symbol": "XAUUSD", "user_id": "u1",
        "lot_size": 0.05,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        # No explanation_snapshot
    }
    activity = {"final_action": "BUY", "final_confidence": 75,
                "tick_id": "tk1",
                "steps": [{"agent": "strategy", "status": "ok", "summary": "BUY 75%"}]}
    db = MagicMock()
    db.agent_activity.find_one = AsyncMock(return_value=activity)
    out = await explain_trade(db, trade)
    assert out["tick_id"] == "tk1"
    assert out["live"] is True
    assert out["entry"]["confidence"] == 75


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
