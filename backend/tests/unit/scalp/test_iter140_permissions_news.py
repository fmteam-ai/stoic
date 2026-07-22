"""iter-140 · Scalp permissions — news guard uses when_ts/when (not the
non-existent time/timestamp keys), regime classified before gates, feed-down
fails closed."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from scalp import permissions


CFG = SimpleNamespace(session_start_utc=0, session_end_utc=24,
                      news_blackout_minutes=15, pip_size=0.0001)


def _db_with_bars(n=24):
    bars = [{"c": 1.10 + i * 0.0001} for i in range(n)]
    db = MagicMock()
    db.intraday_candles.find_one = AsyncMock(return_value={"bars": bars})
    return db


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _feed_ok():
    return {"provider": "ForexFactory (weekly XML)", "status": "OK",
            "cached_events": 5, "last_fetch_age_min": 1.0,
            "last_fetch_at": "x", "last_error": None, "timezone": "UTC"}


def test_high_impact_event_with_when_ts_does_not_fail_closed():
    ev = {"title": "CPI", "country": "USD", "impact": "high",
          "when_ts": time.time() + 6 * 3600,
          "when": "2099-01-01T00:00:00+00:00"}
    with patch("economic_calendar.upcoming_for", new=AsyncMock(return_value=[ev])), \
         patch("economic_calendar.feed_status", new=_feed_ok):
        perms = _run(permissions._compute(_db_with_bars(), "u1", "EURUSD", CFG))
    assert "news event timestamp invalid — fail closed" not in perms["reasons"]
    assert perms["news"]["next_high_impact"]["title"] == "CPI"
    assert perms["regime"] in ("TRENDING_UP", "TRENDING_DOWN", "FLAT", "VOLATILITY_SHOCK")


def test_event_inside_blackout_blocks():
    ev = {"title": "NFP", "country": "USD", "impact": "high",
          "when_ts": time.time() + 5 * 60}
    with patch("economic_calendar.upcoming_for", new=AsyncMock(return_value=[ev])), \
         patch("economic_calendar.feed_status", new=_feed_ok):
        perms = _run(permissions._compute(_db_with_bars(), "u1", "EURUSD", CFG))
    assert any("news blackout" in r for r in perms["reasons"])
    assert perms["regime"] != "UNKNOWN"  # regime still classified


def test_genuinely_untimeable_event_fails_closed():
    ev = {"title": "??", "country": "USD", "impact": "high",
          "when": "not-a-date"}
    with patch("economic_calendar.upcoming_for", new=AsyncMock(return_value=[ev])), \
         patch("economic_calendar.feed_status", new=_feed_ok):
        perms = _run(permissions._compute(_db_with_bars(), "u1", "EURUSD", CFG))
    assert "news event timestamp invalid — fail closed" in perms["reasons"]


def test_feed_down_fails_closed():
    def _down():
        d = _feed_ok(); d["status"] = "DOWN"; d["cached_events"] = 0
        return d
    with patch("economic_calendar.upcoming_for", new=AsyncMock(return_value=[])), \
         patch("economic_calendar.feed_status", new=_down):
        perms = _run(permissions._compute(_db_with_bars(), "u1", "EURUSD", CFG))
    assert "news feed unavailable — fail closed" in perms["reasons"]


def test_insufficient_bars_explains_unknown_regime():
    with patch("economic_calendar.upcoming_for", new=AsyncMock(return_value=[])), \
         patch("economic_calendar.feed_status", new=_feed_ok):
        perms = _run(permissions._compute(_db_with_bars(3), "u1", "EURUSD", CFG))
    assert perms["regime"] == "UNKNOWN"
    assert "insufficient M15 history (3/12 bars)" in (perms["regime_reason"] or "")


def test_stale_cache_message_explains_tick_dependency():
    out = permissions.get_cached("nouser", "EURUSD")
    assert out["regime"] == "UNKNOWN"
    assert "tick stream" in (out.get("regime_reason") or "")
