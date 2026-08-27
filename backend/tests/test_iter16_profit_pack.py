"""Iter-16: P0 Profitability Pack — SL cooldown, liquidity-window booster,
and pre-news existing-position protector.

Verifies new edges layered on top of the existing 10-layer veto cascade.
Uses the same synchronous-wrapper pattern as test_iter14_gold_edge.py
(asyncio.run + mocks) rather than pytest-asyncio (not installed in the env).
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, _BACKEND_DIR)


def _arun(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_trades_collection(docs):
    """Build a Motor-style trades collection where find().sort().limit() is sync
    chain and `.to_list()` is async — mirrors real Motor semantics."""
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=docs)
    chain = MagicMock()
    chain.sort.return_value.limit.return_value = cursor
    trades = MagicMock()
    trades.find = MagicMock(return_value=chain)
    trades.update_one = AsyncMock()
    return trades, cursor


def _make_db_with_open_trades(open_docs):
    """For `find(...).to_list(...)` style queries (no sort/limit chain)."""
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=open_docs)
    trades = MagicMock()
    trades.find = MagicMock(return_value=cursor)
    trades.update_one = AsyncMock()
    db = MagicMock()
    db.trades = trades
    return db, cursor


# ---------------------------------------------------------------------------
# SL cooldown
# ---------------------------------------------------------------------------
def test_sl_cooldown_blocks_recent_stopout():
    from bot_runner import _on_sl_cooldown

    closed_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    trades, _ = _make_trades_collection([{
        "_id": "trade-x",
        "user_id": "u1",
        "symbol": "XAUUSD",
        "status": "closed",
        "close_reason": "stop_loss",
        "closed_at": closed_at,
    }])
    db = MagicMock()
    db.trades = trades
    out = _arun(_on_sl_cooldown(db, "u1", "XAUUSD", lookback_min=45))
    assert out is not None
    assert out["age_minutes"] < 45
    assert out["resumes_in_min"] > 0
    assert out["lookback_minutes"] == 45


def test_sl_cooldown_lets_old_stopout_through():
    from bot_runner import _on_sl_cooldown

    closed_at = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
    trades, _ = _make_trades_collection([{
        "_id": "trade-x", "status": "closed", "close_reason": "stop_loss",
        "closed_at": closed_at,
    }])
    db = MagicMock()
    db.trades = trades
    out = _arun(_on_sl_cooldown(db, "u1", "XAUUSD", lookback_min=45))
    assert out is None


def test_sl_cooldown_empty_when_no_recent_sl_trade():
    """Cursor returns no docs (e.g. last trade was a TP close) — should pass through."""
    from bot_runner import _on_sl_cooldown
    trades, _ = _make_trades_collection([])
    db = MagicMock()
    db.trades = trades
    out = _arun(_on_sl_cooldown(db, "u1", "XAUUSD", lookback_min=45))
    assert out is None


def test_sl_cooldown_disabled_when_minutes_zero():
    from bot_runner import _on_sl_cooldown
    trades, _ = _make_trades_collection([])
    db = MagicMock()
    db.trades = trades
    out = _arun(_on_sl_cooldown(db, "u1", "XAUUSD", lookback_min=0))
    assert out is None


# ---------------------------------------------------------------------------
# Liquidity-window booster
# ---------------------------------------------------------------------------
def test_liquidity_window_high_volume_overlap_detected():
    from microstructure import current_session, session_bias_for

    overlap_dt = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
    sess = current_session(overlap_dt)
    assert sess["is_high_volume_window"] is True
    assert sess["primary"] == "london_ny_overlap"
    bias = session_bias_for("XAUUSD", sess)
    assert bias["preferred_strategy"] == "trend_following"


def test_liquidity_window_off_hours_weekday():
    from microstructure import current_session
    # 22:30 UTC on Wednesday — between NY close and Tokyo open
    off_dt = datetime(2026, 1, 14, 22, 30, tzinfo=timezone.utc)
    sess = current_session(off_dt)
    assert sess["primary"] == "off-hours"
    assert sess["is_weekend"] is False


def test_liquidity_window_no_op_for_btc():
    from microstructure import session_bias_for, current_session

    overlap_dt = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
    sess = current_session(overlap_dt)
    bias = session_bias_for("BTCUSD", sess)
    assert "gold" not in (bias.get("note") or "").lower()


# ---------------------------------------------------------------------------
# Pre-news position protector
# ---------------------------------------------------------------------------
def test_pre_news_flattens_imminent_high_impact():
    """A HIGH-impact NFP 3 min away → trade flattened with FULL_CLOSE."""
    import position_protector
    user_id = "u-pre-news"
    nfp_ts = datetime.now(timezone.utc).timestamp() + 180
    fake_event = {"title": "Non-Farm Payrolls", "country": "USD",
                  "impact": "high", "when_ts": nfp_ts}

    db, _ = _make_db_with_open_trades([{
        "_id": "trade-1", "user_id": user_id, "symbol": "XAUUSD",
        "status": "open", "close_requested": False,
    }])

    async def run():
        with patch("position_protector.upcoming_for",
                   AsyncMock(return_value=[fake_event])):
            return await position_protector.sweep_user(
                db, user_id,
                {"pre_news_protect_enabled": True, "pre_news_protect_minutes": 5},
            )

    flattened = _arun(run())
    assert flattened == 1
    assert db.trades.update_one.await_count == 1
    args, _ = db.trades.update_one.await_args
    set_block = args[1]["$set"]
    assert set_block["close_requested"] is True
    assert set_block["pending_modification"]["type"] == "FULL_CLOSE"
    assert set_block["pending_modification"]["reason"] == "pre_news_protect"


def test_pre_news_disabled_short_circuits():
    """When the toggle is OFF, no event lookup or update should occur."""
    import position_protector

    db, _ = _make_db_with_open_trades([])

    async def run():
        with patch("position_protector.upcoming_for",
                   AsyncMock(return_value=[])) as mock_up:
            n = await position_protector.sweep_user(
                db, "u1",
                {"pre_news_protect_enabled": False, "pre_news_protect_minutes": 5},
            )
            return n, mock_up

    flattened, mock_up = _arun(run())
    assert flattened == 0
    mock_up.assert_not_called()


def test_pre_news_ignores_medium_impact():
    import position_protector
    user_id = "u-med"
    soon_ts = datetime.now(timezone.utc).timestamp() + 120
    fake_event = {"title": "Building Permits", "country": "USD",
                  "impact": "medium", "when_ts": soon_ts}

    db, _ = _make_db_with_open_trades([{
        "_id": "trade-2", "user_id": user_id, "symbol": "XAUUSD", "status": "open",
    }])

    async def run():
        with patch("position_protector.upcoming_for",
                   AsyncMock(return_value=[fake_event])):
            return await position_protector.sweep_user(
                db, user_id,
                {"pre_news_protect_enabled": True, "pre_news_protect_minutes": 5},
            )

    assert _arun(run()) == 0
    db.trades.update_one.assert_not_called()


def test_pre_news_ignores_events_beyond_window():
    """HIGH-impact event 30 min away with 5-min window → no action."""
    import position_protector
    user_id = "u-far"
    far_ts = datetime.now(timezone.utc).timestamp() + 30 * 60
    fake_event = {"title": "CPI", "country": "USD",
                  "impact": "high", "when_ts": far_ts}

    db, _ = _make_db_with_open_trades([{
        "_id": "trade-3", "user_id": user_id, "symbol": "XAUUSD", "status": "open",
    }])

    async def run():
        with patch("position_protector.upcoming_for",
                   AsyncMock(return_value=[fake_event])):
            return await position_protector.sweep_user(
                db, user_id,
                {"pre_news_protect_enabled": True, "pre_news_protect_minutes": 5},
            )

    assert _arun(run()) == 0


# ---------------------------------------------------------------------------
# Intelligence counter registration
# ---------------------------------------------------------------------------
def test_new_counters_registered():
    from intelligence_counters import CATEGORIES
    assert "sl_cooldown_block" in CATEGORIES
    assert "pre_news_protect" in CATEGORIES


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
