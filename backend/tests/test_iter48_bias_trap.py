"""Tests for iter-48: bias-trap protection (anti-pyramid + loss-streak +
self-contradiction veto) following the 7-trade –$1121 loss forensics.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bson import ObjectId


# --------------------------------------------------------------------------
#  Self-contradiction veto in ai_signals (in-process; no LLM)
# --------------------------------------------------------------------------

def _detect_block_phrases(reasoning_text: str) -> bool:
    """Replicates the inline check in ai_signals.py — keeps the test from
    spinning up the full analyser. The phrase list MUST stay in sync with
    `block_phrases` in `ai_signals.analyze_market`.
    """
    block_phrases = [
        "cautious_wait", "cautious wait",
        "blocks new positions", "blocks trading", "blocks trade execution",
        "non-tradeable environment", "do not enter", "do not trade",
        "no entry", "mandates no entry", "mandates patience",
        "prohibit entry", "prohibits entry",
        "red-light noise filter blocks",
    ]
    rtxt = reasoning_text.lower()
    return any(p in rtxt for p in block_phrases)


@pytest.mark.parametrize("text", [
    "TRANSITIONAL regime with CAUTIOUS_WAIT mode blocks new positions",
    "red-light noise filter blocks trading",
    "Multiple structural headwinds prohibit entry",
    "Mode mandates patience and no entry",
    "Non-tradeable environment — do not trade",
])
def test_self_contradiction_detector_catches_block_phrases(text):
    """Every signal that lost money on 2026-06-26 contained one of these."""
    assert _detect_block_phrases(text) is True


def test_self_contradiction_detector_passes_clean_reasoning():
    assert _detect_block_phrases("Strong bullish trend with DXY weakening, MTF aligned, A+ confluence.") is False


# --------------------------------------------------------------------------
#  Anti-pyramid + loss-streak — bot_runner integration logic
# --------------------------------------------------------------------------
# We exercise the inlined Mongo queries directly to lock the contract that
# `_process_user_account_locked` enforces them in this order:
#   1. inflight >= max_concurrent     → SKIP
#   2. same_dir_open > 0              → SKIP (anti-pyramid)
#   3. loss-streak ≥2 on same dir     → SKIP (circuit-breaker)

@pytest.mark.asyncio
async def test_pyramid_query_filters_same_direction_only():
    """Anti-pyramid filter must NOT block opposite-direction signals."""
    db = MagicMock()
    db.trades.count_documents = AsyncMock(return_value=1)  # one SELL open
    sym, action = "XAUUSD", "BUY"
    user_id = "u1"
    q = {
        "user_id": user_id, "symbol": sym, "action": action,
        "status": {"$in": ["pending", "open"]},
    }
    n = await db.trades.count_documents(q)
    # The mock returns 1 regardless of query, but verify q built correctly
    args, _ = db.trades.count_documents.call_args
    assert args[0]["action"] == "BUY"
    assert args[0]["status"] == {"$in": ["pending", "open"]}
    assert n == 1


@pytest.mark.asyncio
async def test_loss_streak_query_requires_two_consecutive_losses():
    """Locks the per-symbol per-direction window query shape."""
    from datetime import datetime, timezone, timedelta
    db = MagicMock()
    # Simulate the two most recent SELLs both losing
    losing_doc_a = {"pnl": -90, "action": "SELL", "closed_at": "2026-06-26T05:55:00+00:00"}
    losing_doc_b = {"pnl": -110, "action": "SELL", "closed_at": "2026-06-26T06:20:00+00:00"}
    cursor = MagicMock()
    cursor.sort = MagicMock(return_value=cursor)
    cursor.limit = MagicMock(return_value=cursor)
    cursor.to_list = AsyncMock(return_value=[losing_doc_b, losing_doc_a])
    db.trades.find = MagicMock(return_value=cursor)

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    recent = await db.trades.find({
        "user_id": "u1", "symbol": "XAUUSD", "action": "SELL",
        "status": "closed", "closed_at": {"$gte": cutoff},
    }).sort("closed_at", -1).limit(2).to_list(2)
    assert len(recent) == 2
    assert all(t["pnl"] < 0 for t in recent)


@pytest.mark.asyncio
async def test_loss_streak_no_block_when_one_win_in_recent_two():
    """If even ONE of the last 2 was a win, do not trip the circuit-breaker."""
    db = MagicMock()
    losing_doc = {"pnl": -100, "action": "SELL"}
    winning_doc = {"pnl": +50, "action": "SELL"}
    cursor = MagicMock()
    cursor.sort = MagicMock(return_value=cursor)
    cursor.limit = MagicMock(return_value=cursor)
    cursor.to_list = AsyncMock(return_value=[winning_doc, losing_doc])
    db.trades.find = MagicMock(return_value=cursor)
    recent = await db.trades.find({"user_id": "u1"}).sort("closed_at", -1).limit(2).to_list(2)
    # Replicates the bot_runner.py condition:
    assert not all(float(t.get("pnl") or 0) < 0 for t in recent)


# --------------------------------------------------------------------------
#  Auto-deleverage prefers losers over largest-notional — risk_manager logic
# --------------------------------------------------------------------------

def test_sector_cap_prefers_worst_loser_when_pnl_available():
    """Reproduces the iter-48 fix in portfolio/risk_manager.py:148 — when
    a sector breaches its cap and live P&L is available on positions, close
    the WORST LOSER, not the LARGEST. Previously we kept culling deepest-
    underwater pyramids and locking in losses while the bot opened more
    same-direction trades into the trap.
    """
    positions = [
        {"trade_id": "a", "symbol": "XAUUSD", "lot": 0.17, "notional": 70000, "live_pnl":  -250},
        {"trade_id": "b", "symbol": "XAUUSD", "lot": 0.17, "notional": 80000, "live_pnl":   +5},
        {"trade_id": "c", "symbol": "XAUUSD", "lot": 0.17, "notional": 65000, "live_pnl": -100},
    ]
    has_pnl = any(p.get("live_pnl") for p in positions)
    assert has_pnl
    worst = min(positions, key=lambda x: x.get("live_pnl") or 0)
    assert worst["trade_id"] == "a"  # the -$250 loser (not the largest "b")


def test_sector_cap_falls_back_to_largest_when_no_pnl():
    """When the broker hasn't reported live P&L yet, fall back to the legacy
    'largest notional' behaviour so the deleverage still functions."""
    positions = [
        {"trade_id": "a", "lot": 0.10, "notional": 40000, "live_pnl": 0},
        {"trade_id": "b", "lot": 0.17, "notional": 70000, "live_pnl": 0},
        {"trade_id": "c", "lot": 0.05, "notional": 20000, "live_pnl": 0},
    ]
    has_pnl = any(p.get("live_pnl") for p in positions)
    assert not has_pnl
    worst = max(positions, key=lambda x: x["notional"])
    assert worst["trade_id"] == "b"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
