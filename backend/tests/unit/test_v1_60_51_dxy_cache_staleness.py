"""v1.60.51 — DXY cache staleness. macro/dxy.get_dxy_snapshot keeps returning the old dxy_cache document when the
download fails and entry_context.cached_dxy never looked at fetched_at, so a week-old regime could be stamped on new
entries. Field names (current_price / regime / fetched_at) are unchanged — the monitoring agent's "value/source null"
was a wrong-field query, not a writer bug."""
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
NOW = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)   # a Monday morning


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _doc(age_h, now=NOW):
    return {"_id": "latest", "current_price": 102.07, "regime": "bullish_usd", "ema_20": 101.5,
            "fetched_at": (now - timedelta(hours=age_h)).isoformat()}


def _live(age_h):            # relative to the real clock — cached_dxy() uses datetime.now()
    return _doc(age_h, datetime.now(timezone.utc))


def test_age_helpers_tolerate_a_weekend_but_not_beyond_48h():
    from entry_context import DXY_MAX_AGE_HOURS, dxy_cache_age_hours, dxy_cache_is_stale
    assert DXY_MAX_AGE_HOURS == 48
    assert round(dxy_cache_age_hours(_doc(40), NOW), 3) == 40.0
    assert dxy_cache_is_stale(_doc(40), NOW) is False          # Friday evening fetch, Sunday night read
    assert dxy_cache_is_stale(_doc(49), NOW) is True
    assert dxy_cache_is_stale(None, NOW) is True
    assert dxy_cache_is_stale({"fetched_at": "garbage"}, NOW) is True
    assert dxy_cache_is_stale({"fetched_at": (NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()}, NOW) is False


def test_cached_dxy_returns_fresh_fields_and_none_when_stale(caplog):
    import entry_context as ec
    db = FakeDb()
    db.dxy_cache.rows.append(_live(10))
    with patch("database.get_db", lambda: db):
        assert _run(ec.cached_dxy()) == {"current_price": 102.07, "regime": "bullish_usd"}
    db.dxy_cache.rows[0] = _live(72)
    ec._last_stale_warn = 0.0
    with patch("database.get_db", lambda: db), caplog.at_level(logging.WARNING, logger="entry_context"):
        assert _run(ec.cached_dxy()) is None
        assert _run(ec.cached_dxy()) is None                   # second call within the hour: no duplicate warning
    warns = [r for r in caplog.records if "DXY cache stale" in r.getMessage()]
    assert len(warns) == 1 and r"> 48h" in warns[0].getMessage()


def test_entry_context_stamps_dxy_unknown_when_cache_is_stale():
    import entry_context as ec
    db = FakeDb()
    db.dxy_cache.rows.append(_live(100))

    async def no_fred():
        return {}
    with patch("database.get_db", lambda: db), patch.object(ec, "cached_fred_series", no_fred):
        ctx = _run(ec.build_entry_context({"symbol": "XAUUSD", "action": "BUY"}))
    assert not ctx.get("dxy") and (ctx.get("dxy_regime") in (None, "unknown"))
    db.dxy_cache.rows[0] = _live(5)
    with patch("database.get_db", lambda: db), patch.object(ec, "cached_fred_series", no_fred):
        ctx2 = _run(ec.build_entry_context({"symbol": "XAUUSD", "action": "BUY"}))
    assert ctx2.get("dxy") == 102.07 and ctx2.get("dxy_regime") == "bullish_usd"


def test_bot_health_raises_amber_issue_for_stale_dxy_cache():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            "routes", "bot_routes.py")).read()
    seg = src[src.index('"code": "dxy_cache_stale"') - 700:src.index('"code": "dxy_cache_stale"') + 400]
    assert '"severity": "warning"' in seg and "score -= 5" in seg and "_dxy_age > DXY_MAX_AGE_HOURS" in seg


def test_dxy_writer_field_names_unchanged():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            "macro", "dxy.py")).read()
    for f in ('"current_price"', '"ema_20"', '"slope_5d_pct"', '"above_ema"', '"regime"', '"as_of_date"', '"fetched_at"'):
        assert f in src
    assert '"value"' not in src and '"source"' not in src
