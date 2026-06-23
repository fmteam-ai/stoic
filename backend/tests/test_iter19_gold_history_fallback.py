"""Iter-19: multi-source XAU history fallback (Yahoo → Stooq → CoinGecko → Mongo)

Locks in the fix that recovers the bot from CoinGecko 429 rate limits by
adding Yahoo Finance (GC=F) as the primary source and MongoDB persistence
as last-resort fallback.
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/app/backend")


def _arun(coro):
    return asyncio.run(coro)


def _fake_history(n=120):
    return [{"date": f"2026-01-{i:02d}", "open": 2000+i, "high": 2010+i,
             "low": 1990+i, "close": 2005+i, "volume": 0}
            for i in range(1, n+1)]


def test_get_history_uses_yahoo_when_healthy():
    """Yahoo path returns >=100 bars → no Stooq / CG / Mongo fallback called."""
    import market
    market._cache.clear()

    yahoo_mock = AsyncMock(return_value=_fake_history(200))
    stooq_mock = AsyncMock(return_value=[])
    cg_mock = AsyncMock(return_value=[])
    save_mock = AsyncMock()
    load_mock = AsyncMock(return_value=None)

    with patch.object(market, "_yahoo_gold_history", yahoo_mock), \
         patch.object(market, "_stooq_gold_history", stooq_mock), \
         patch.object(market, "_cg_history", cg_mock), \
         patch.object(market, "_history_save_to_mongo", save_mock), \
         patch.object(market, "_history_load_from_mongo", load_mock):
        h = _arun(market.get_history("XAUUSD"))

    assert len(h) >= 100
    yahoo_mock.assert_awaited_once()
    stooq_mock.assert_not_awaited()
    cg_mock.assert_not_awaited()
    save_mock.assert_awaited_once()
    load_mock.assert_not_awaited()


def test_get_history_falls_back_to_stooq():
    """Yahoo raises → Stooq succeeds → no CG / Mongo fallback."""
    import market
    market._cache.clear()

    yahoo_mock = AsyncMock(side_effect=RuntimeError("Yahoo down"))
    stooq_mock = AsyncMock(return_value=_fake_history(200))
    cg_mock = AsyncMock(return_value=[])
    save_mock = AsyncMock()
    load_mock = AsyncMock(return_value=None)

    with patch.object(market, "_yahoo_gold_history", yahoo_mock), \
         patch.object(market, "_stooq_gold_history", stooq_mock), \
         patch.object(market, "_cg_history", cg_mock), \
         patch.object(market, "_history_save_to_mongo", save_mock), \
         patch.object(market, "_history_load_from_mongo", load_mock):
        h = _arun(market.get_history("XAUUSD"))

    assert len(h) >= 100
    stooq_mock.assert_awaited_once()
    cg_mock.assert_not_awaited()


def test_get_history_falls_back_to_coingecko():
    """Yahoo + Stooq both fail → CoinGecko used (last live source)."""
    import market
    market._cache.clear()

    yahoo_mock = AsyncMock(side_effect=RuntimeError("Yahoo down"))
    stooq_mock = AsyncMock(side_effect=RuntimeError("Stooq down"))
    cg_mock = AsyncMock(return_value=_fake_history(200))
    save_mock = AsyncMock()
    load_mock = AsyncMock(return_value=None)

    with patch.object(market, "_yahoo_gold_history", yahoo_mock), \
         patch.object(market, "_stooq_gold_history", stooq_mock), \
         patch.object(market, "_cg_history", cg_mock), \
         patch.object(market, "_history_save_to_mongo", save_mock), \
         patch.object(market, "_history_load_from_mongo", load_mock):
        h = _arun(market.get_history("XAUUSD"))

    assert len(h) >= 100
    cg_mock.assert_awaited_once()


def test_get_history_uses_mongo_when_all_upstreams_fail():
    """Every live source 429s → recover from Mongo cache → bot keeps trading."""
    import market
    import httpx
    market._cache.clear()

    yahoo_mock = AsyncMock(side_effect=httpx.HTTPStatusError(
        "429", request=MagicMock(), response=MagicMock(status_code=429)))
    stooq_mock = AsyncMock(side_effect=httpx.HTTPStatusError(
        "429", request=MagicMock(), response=MagicMock(status_code=429)))
    cg_mock = AsyncMock(side_effect=httpx.HTTPStatusError(
        "429", request=MagicMock(), response=MagicMock(status_code=429)))
    save_mock = AsyncMock()
    load_mock = AsyncMock(return_value=_fake_history(200))

    with patch.object(market, "_yahoo_gold_history", yahoo_mock), \
         patch.object(market, "_stooq_gold_history", stooq_mock), \
         patch.object(market, "_cg_history", cg_mock), \
         patch.object(market, "_history_save_to_mongo", save_mock), \
         patch.object(market, "_history_load_from_mongo", load_mock):
        h = _arun(market.get_history("XAUUSD"))

    assert len(h) >= 100
    load_mock.assert_awaited_once()
    save_mock.assert_not_awaited()  # nothing fresh to save


def test_get_history_raises_only_when_mongo_is_also_empty():
    """Every source down AND no Mongo cache → raise so the caller logs the issue."""
    import market
    import httpx
    market._cache.clear()

    yahoo_mock = AsyncMock(side_effect=httpx.HTTPStatusError(
        "429", request=MagicMock(), response=MagicMock(status_code=429)))
    stooq_mock = AsyncMock(side_effect=httpx.HTTPStatusError(
        "429", request=MagicMock(), response=MagicMock(status_code=429)))
    cg_mock = AsyncMock(side_effect=httpx.HTTPStatusError(
        "429", request=MagicMock(), response=MagicMock(status_code=429)))
    save_mock = AsyncMock()
    load_mock = AsyncMock(return_value=None)

    with patch.object(market, "_yahoo_gold_history", yahoo_mock), \
         patch.object(market, "_stooq_gold_history", stooq_mock), \
         patch.object(market, "_cg_history", cg_mock), \
         patch.object(market, "_history_save_to_mongo", save_mock), \
         patch.object(market, "_history_load_from_mongo", load_mock):
        try:
            _arun(market.get_history("XAUUSD"))
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "XAUUSD" in str(e) or "Failed" in str(e) or "failed" in str(e)
