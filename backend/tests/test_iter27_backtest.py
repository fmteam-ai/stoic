"""Tests for the Strategy Backtest Preview (strategy_backtest.run_backtest).

Uses a mocked db so we don't depend on a live MongoDB instance during unit tests.
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

from strategy_backtest import run_backtest, _in_session


def _hours_ago_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _fake_db(trades: list[dict]):
    """Mock db.trades.find(...).sort(...).limit(...).to_list(length=...)."""
    # We need to honour the query — at minimum filter user_id/status/symbol/closed_at.
    def _match(q, t):
        if q.get("user_id") and t.get("user_id") != q["user_id"]:
            return False
        if q.get("status") and t.get("status") != q["status"]:
            return False
        sym_q = q.get("symbol")
        if isinstance(sym_q, dict) and "$in" in sym_q:
            if t.get("symbol") not in sym_q["$in"]:
                return False
        elif sym_q and t.get("symbol") != sym_q:
            return False
        closed_q = q.get("closed_at")
        if isinstance(closed_q, dict) and "$gte" in closed_q:
            if str(t.get("closed_at") or "") < closed_q["$gte"]:
                return False
        return True

    class _Cursor:
        def __init__(self, items):
            self.items = items
        def sort(self, *_a, **_kw):
            return self
        def limit(self, n):
            self.items = self.items[:n]
            return self
        async def to_list(self, length=None):
            return self.items[:length] if length else self.items

    def _find(q):
        return _Cursor([t for t in trades if _match(q, t)])

    db = MagicMock()
    db.trades.find = MagicMock(side_effect=_find)
    return db


# ---------- session window helper ----------
def test_in_session_london():
    assert _in_session(10, "london") is True
    assert _in_session(6, "london") is False
    assert _in_session(16, "london") is False


def test_in_session_tokyo_wraps_midnight():
    assert _in_session(2, "tokyo") is True
    assert _in_session(23, "tokyo") is True
    assert _in_session(12, "tokyo") is False


def test_in_session_any():
    assert _in_session(0, "any") is True
    assert _in_session(23, "any") is True


def _make_trade(symbol, pnl, hours_ago):
    ts = _hours_ago_iso(hours_ago)
    return {
        "user_id": "u1", "status": "closed", "symbol": symbol,
        "pnl": pnl, "opened_at": ts, "closed_at": ts,
    }


# ---------- backtest runner ----------
@pytest.mark.asyncio
async def test_no_trades_returns_empty_with_note(monkeypatch):
    monkeypatch.setattr("strategy_backtest.get_db", lambda: _fake_db([]))
    result = await run_backtest(
        compiled={"symbols": ["XAUUSD"]}, user_id="u1", lookback_days=30,
    )
    assert result["matched_trades"] == 0
    assert result["win_rate"] is None
    assert any("paper mode" in n.lower() or "no closed trades" in n.lower()
               for n in result["notes"])


@pytest.mark.asyncio
async def test_symbol_filter_restricts(monkeypatch):
    trades = [
        _make_trade("XAUUSD", 10.0, 10),
        _make_trade("XAUUSD", -5.0, 20),
        _make_trade("BTCUSD", 50.0, 15),
    ]
    monkeypatch.setattr("strategy_backtest.get_db", lambda: _fake_db(trades))
    result = await run_backtest(
        compiled={"symbols": ["XAUUSD"]}, user_id="u1", lookback_days=30,
    )
    assert result["matched_trades"] == 2
    assert result["wins"] == 1
    assert result["losses"] == 1
    assert result["total_pnl_usd"] == 5.0


@pytest.mark.asyncio
async def test_aggregate_metrics(monkeypatch):
    trades = [
        _make_trade("XAUUSD",  10.0, 10),
        _make_trade("XAUUSD",  20.0, 11),
        _make_trade("XAUUSD", -15.0, 12),
        _make_trade("BTCUSD",  50.0, 13),
        _make_trade("BTCUSD", -10.0, 14),
    ]
    monkeypatch.setattr("strategy_backtest.get_db", lambda: _fake_db(trades))
    result = await run_backtest(
        compiled={"symbols": ["XAUUSD", "BTCUSD"]},
        user_id="u1", lookback_days=30,
    )
    assert result["matched_trades"] == 5
    assert result["wins"] == 3
    assert result["losses"] == 2
    assert result["total_pnl_usd"] == pytest.approx(55.0)
    assert result["avg_pnl_usd"] == pytest.approx(11.0)
    assert result["best_trade"] == 50.0
    assert result["worst_trade"] == -15.0
    assert result["by_symbol"]["XAUUSD"]["trades"] == 3
    assert result["by_symbol"]["BTCUSD"]["trades"] == 2
    assert result["by_symbol"]["XAUUSD"]["pnl"] == 15.0


@pytest.mark.asyncio
async def test_small_sample_note_fires(monkeypatch):
    trades = [_make_trade("XAUUSD", 1.0, i) for i in range(5)]
    monkeypatch.setattr("strategy_backtest.get_db", lambda: _fake_db(trades))
    result = await run_backtest(
        compiled={"symbols": ["XAUUSD"]}, user_id="u1", lookback_days=30,
    )
    assert result["matched_trades"] == 5
    assert any("sample" in n.lower() for n in result["notes"])
