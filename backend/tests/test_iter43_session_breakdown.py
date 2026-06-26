"""Tests for iter-43: per-session performance breakdown.

Covers `analytics.compute_sessions()`:
  - 5-bucket split (Asia / London / Overlap / NY / Off) by UTC hour at open.
  - R-multiple math: R = pnl / (|entry - SL| × lot × contract_size).
  - Always emits all 5 buckets, even when count=0.
  - Headline ranks require ≥3 sampled trades to qualify.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import analytics


def _make_trade(*, opened_at, pnl, entry, sl, lot, symbol="XAUUSD"):
    return {
        "user_id": "u1", "status": "closed", "symbol": symbol,
        "pnl": pnl, "entry_price": entry, "stop_loss": sl, "lot_size": lot,
        "opened_at": opened_at,
    }


# -----------------------------------------------------------
# R-multiple math
# -----------------------------------------------------------
def test_r_multiple_xauusd_winner():
    # XAUUSD: contract = 100. Entry 2000 / SL 1990 → risk = 10 × 0.1 × 100 = $100.
    # PnL +$200 → R = 2.0
    row = {"symbol": "XAUUSD", "entry_price": 2000, "stop_loss": 1990,
           "lot_size": 0.1, "pnl": 200.0}
    assert analytics._r_multiple(row) == 2.0


def test_r_multiple_btcusd_loser():
    # BTCUSD: contract = 1. Entry 60000 / SL 59000 → risk = 1000 × 0.01 × 1 = $10.
    # PnL -$15 → R = -1.5
    row = {"symbol": "BTCUSD", "entry_price": 60000, "stop_loss": 59000,
           "lot_size": 0.01, "pnl": -15.0}
    assert analytics._r_multiple(row) == -1.5


def test_r_multiple_returns_none_when_sl_missing():
    row = {"symbol": "XAUUSD", "entry_price": 2000, "stop_loss": 0,
           "lot_size": 0.1, "pnl": 100.0}
    assert analytics._r_multiple(row) is None


def test_r_multiple_clamps_extreme_outliers():
    # Tiny stop, huge pnl → would be 1000R; cap at +10R.
    row = {"symbol": "XAUUSD", "entry_price": 2000, "stop_loss": 1999.9,
           "lot_size": 0.01, "pnl": 1000.0}
    assert analytics._r_multiple(row) == 10.0


# -----------------------------------------------------------
# Session bucket boundaries
# -----------------------------------------------------------
@pytest.mark.parametrize("hour,expected", [
    (0, "ASIA"), (3, "ASIA"), (6, "ASIA"),
    (7, "LONDON"), (10, "LONDON"), (12, "LONDON"),
    (13, "OVERLAP"), (14, "OVERLAP"), (15, "OVERLAP"),
    (16, "NY"), (19, "NY"), (20, "NY"),
    (21, "OFF"), (23, "OFF"),
])
def test_session_bucket_boundaries(hour, expected):
    from datetime import datetime, timezone
    dt = datetime(2026, 6, 26, hour, 30, tzinfo=timezone.utc)
    assert analytics._session_key_4buckets(dt) == expected


# -----------------------------------------------------------
# compute_sessions end-to-end
# -----------------------------------------------------------
@pytest.mark.asyncio
async def test_compute_sessions_emits_all_five_buckets_when_no_trades():
    db = MagicMock()
    db.trades.find = MagicMock(
        return_value=MagicMock(to_list=AsyncMock(return_value=[])),
    )
    with patch("analytics.get_db", return_value=db):
        result = await analytics.compute_sessions("u1")
    keys = [b["key"] for b in result["buckets"]]
    assert keys == ["ASIA", "LONDON", "OVERLAP", "NY", "OFF"]
    assert result["overall"]["count"] == 0
    assert result["best_session_by_r"] is None
    assert result["best_session_by_pnl"] is None


@pytest.mark.asyncio
async def test_compute_sessions_buckets_and_ranks():
    # 3 winners in London + 3 losers in NY → London should top both ranks.
    trades = [
        _make_trade(opened_at="2026-06-26T08:00:00+00:00", pnl=+100, entry=2000, sl=1995, lot=0.1),
        _make_trade(opened_at="2026-06-26T09:00:00+00:00", pnl=+100, entry=2000, sl=1995, lot=0.1),
        _make_trade(opened_at="2026-06-26T10:00:00+00:00", pnl=+100, entry=2000, sl=1995, lot=0.1),
        _make_trade(opened_at="2026-06-26T17:00:00+00:00", pnl=-100, entry=2000, sl=1995, lot=0.1),
        _make_trade(opened_at="2026-06-26T18:00:00+00:00", pnl=-100, entry=2000, sl=1995, lot=0.1),
        _make_trade(opened_at="2026-06-26T19:00:00+00:00", pnl=-100, entry=2000, sl=1995, lot=0.1),
    ]
    db = MagicMock()
    db.trades.find = MagicMock(
        return_value=MagicMock(to_list=AsyncMock(return_value=trades)),
    )
    with patch("analytics.get_db", return_value=db):
        result = await analytics.compute_sessions("u1")

    by_key = {b["key"]: b for b in result["buckets"]}
    # London: 3 wins, +$300 total, +2R each → avg_r = 2.0
    assert by_key["LONDON"]["count"] == 3
    assert by_key["LONDON"]["win_rate"] == 100.0
    assert by_key["LONDON"]["total_pnl"] == 300.0
    assert by_key["LONDON"]["avg_r"] == 2.0
    # NY: 3 losses, -$300, -2R each → expectancy = -2R
    assert by_key["NY"]["count"] == 3
    assert by_key["NY"]["win_rate"] == 0.0
    assert by_key["NY"]["avg_r"] == -2.0
    # Headline rank — London wins both
    assert result["best_session_by_r"] == "LONDON"
    assert result["best_session_by_pnl"] == "LONDON"
    # Untouched sessions still emit a placeholder bucket
    assert by_key["ASIA"]["count"] == 0
    assert by_key["ASIA"]["avg_r"] is None


@pytest.mark.asyncio
async def test_compute_sessions_rank_requires_min_sample_of_three():
    # Two big wins in Asia, two small wins in London. Asia would lead on avg_r
    # but only has 2 trades — ranking must skip it and pick London.
    trades = [
        _make_trade(opened_at="2026-06-26T01:00:00+00:00", pnl=+500, entry=2000, sl=1995, lot=0.1),
        _make_trade(opened_at="2026-06-26T02:00:00+00:00", pnl=+500, entry=2000, sl=1995, lot=0.1),
        _make_trade(opened_at="2026-06-26T08:00:00+00:00", pnl=+50, entry=2000, sl=1995, lot=0.1),
        _make_trade(opened_at="2026-06-26T09:00:00+00:00", pnl=+50, entry=2000, sl=1995, lot=0.1),
        _make_trade(opened_at="2026-06-26T10:00:00+00:00", pnl=+50, entry=2000, sl=1995, lot=0.1),
    ]
    db = MagicMock()
    db.trades.find = MagicMock(
        return_value=MagicMock(to_list=AsyncMock(return_value=trades)),
    )
    with patch("analytics.get_db", return_value=db):
        result = await analytics.compute_sessions("u1")
    assert result["best_session_by_r"] == "LONDON"
    assert result["best_session_by_pnl"] == "LONDON"
