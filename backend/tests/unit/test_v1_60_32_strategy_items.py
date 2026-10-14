"""v1.60.32 — A4 canonical pattern_key · B7 TIPS single-flight · C8 scalp breakeven 0.5R · C9 90-min time-stop."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── A4 ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_pattern_key_uses_the_canonical_symbol_and_legacy_keys_are_backfilled():
    from loss_postmortem import _pattern_key, normalize_pattern_key, ensure_pattern_keys_canonical
    sig = {"regime": {"regime": "LOW_VOL_TREND"}, "session": {"primary": "london"}}
    assert _pattern_key({"symbol": "GOLD#", "action": "SELL"}, sig) == "XAUUSD|LOW_VOL_TREND|LONDON|SELL"
    assert _pattern_key({"symbol": "XAUUSD.fx", "action": "SELL"}, sig) == _pattern_key({"symbol": "XAUUSD", "action": "SELL"}, sig)
    assert _pattern_key({"symbol": "EURUSD-ECN", "action": "BUY"}, sig).startswith("EURUSD|")
    assert normalize_pattern_key("GOLD#|LOW_VOL_TREND|LONDON|SELL") == "XAUUSD|LOW_VOL_TREND|LONDON|SELL"
    assert normalize_pattern_key(None) is None and normalize_pattern_key("weird") == "weird"
    db = FakeDb()
    db.loss_postmortems.rows += [{"_id": ObjectId(), "pattern_key": "GOLD#|LOW_VOL_TREND|LONDON|SELL"},
                                 {"_id": ObjectId(), "pattern_key": "XAUUSD|LOW_VOL_TREND|LONDON|SELL"}]
    db.guardrail_adjustments.rows += [{"_id": ObjectId(), "pattern_key": "XAUUSD.fx|HIGH_VOL|NY|BUY"}]
    assert _run(ensure_pattern_keys_canonical(db)) == 2
    assert {r["pattern_key"] for r in db.loss_postmortems.rows} == {"XAUUSD|LOW_VOL_TREND|LONDON|SELL"}
    assert db.guardrail_adjustments.rows[0]["pattern_key"] == "XAUUSD|HIGH_VOL|NY|BUY"
    assert db.guardrail_adjustments.rows[0]["pattern_key_legacy"] == "XAUUSD.fx|HIGH_VOL|NY|BUY"
    assert _run(ensure_pattern_keys_canonical(db)) == 0          # idempotent


# ── B7 ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_tips_fetch_is_single_flight_under_concurrency(monkeypatch):
    import macro.tips as tips
    db = FakeDb()
    monkeypatch.setattr(tips, "get_db", lambda: db)
    calls = {"n": 0}

    async def fake_fetch_year(year):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return [{"date": f"{year}-10-0{i}", "real_yield_10y": 1.8 + i / 100} for i in range(1, 8)]

    monkeypatch.setattr(tips, "_fetch_year", fake_fetch_year)

    async def main():
        return await asyncio.gather(*[tips.get_real_yield() for _ in range(4)])

    out = _run(main())
    assert all(o and o["key"] == "10Y_REAL" for o in out)
    assert calls["n"] == 1, calls                      # one fetch for four concurrent callers
    # a failing fetch: still one attempt, one negative-cache row, not four "failure #1" lines
    db.tips_cache.rows.clear()
    calls["n"] = 0

    async def boom(year):
        calls["n"] += 1
        await asyncio.sleep(0.02)
        raise tips.httpx.ConnectTimeout("vps")

    monkeypatch.setattr(tips, "_fetch_year", boom)
    with patch("macro.fred.fetch_series", AsyncMock(side_effect=RuntimeError("no fred"))):
        out = _run(main())
    assert out == [None, None, None, None] and calls["n"] == 1
    neg = [r for r in db.tips_cache.rows if r["key"] == "10Y_REAL_FAIL"]
    assert len(neg) == 1 and neg[0]["consecutive_failures"] == 1


# ── C8 / C9 ────────────────────────────────────────────────────────────────────────────────────────────────────
def _scalp_trade(**kw):
    t = {"_id": ObjectId(), "user_id": "u1", "symbol": "XAUUSD", "action": "BUY", "engine": "hf_scalp_fast",
         "entry_price": 4000.0, "stop_loss": 3996.0, "sl_pips": 400, "tp_pips": [800, 800, 800], "lot_size": 0.05,
         "original_lot_size": 0.05, "mt5_ticket": 123456789, "status": "open", "mode": "demo", "spread_pips": 2.2,
         "opened_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}
    t.update(kw)
    return t


def test_scalp_breakeven_moves_the_stop_to_entry_plus_costs_at_half_r():
    import trade_manager as tm
    db = FakeDb()
    t = _scalp_trade()
    db.trades.rows.append(t)
    # +0.72R → stop goes to entry + 2.2 pips (0.22 on gold, pip = 0.1) — the trade can no longer take the full stop
    current = 4000.0 + 0.1 * 400 * 0.72
    with patch.object(tm, "ws_manager") as ws:
        ws.broadcast = AsyncMock()
        assert _run(tm._scalp_engine_exits(db, t, {}, current, 400 * 0.72)) is True
    pm = db.trades.rows[0]["pending_modification"]
    assert pm["type"] == "MODIFY_SL" and pm["reason"] == "scalp_breakeven_0_5r"
    assert pm["new_sl"] == 4000.22 == db.trades.rows[0]["be_target_sl"]
    # below +0.5R nothing happens
    t2 = _scalp_trade()
    assert _run(tm._scalp_engine_exits(FakeDb(), t2, {}, 4001.0, 100.0)) is False
    # SELL side mirrors (entry − costs); default cost when the spread was not recorded
    t3 = _scalp_trade(action="SELL", spread_pips=None)
    assert tm.scalp_breakeven_sl(t3) == 4000.0 - 0.1 * 2.5
    assert tm.scalp_breakeven_sl(_scalp_trade(symbol="EURUSD", entry_price=1.1, spread_pips=None)) == round(1.1 + 0.0001 * 1.5, 5)


def test_scalp_time_stop_closes_a_trade_that_never_reached_half_r_within_90_minutes():
    import trade_manager as tm
    now = datetime.now(timezone.utc)
    stale = _scalp_trade(opened_at=(now - timedelta(minutes=95)).isoformat())
    fresh = _scalp_trade(opened_at=(now - timedelta(minutes=60)).isoformat())
    armed = _scalp_trade(opened_at=(now - timedelta(minutes=200)).isoformat(), be_target_sl=4000.22)
    assert tm.scalp_time_stop_due(stale) and not tm.scalp_time_stop_due(fresh) and not tm.scalp_time_stop_due(armed)
    db = FakeDb()
    db.trades.rows.append(stale)
    closed = AsyncMock(return_value={"command_id": "c1", "trades_marked_for_close": 1})
    with patch.object(tm, "request_close", closed), patch.object(tm, "ws_manager") as ws:
        ws.broadcast = AsyncMock()
        assert _run(tm._scalp_engine_exits(db, stale, {}, 3999.0, -100.0)) is True
    assert closed.await_args.kwargs["reason"] == "time_stop" and closed.await_args.kwargs["actor"] == "trade_manager"
    assert closed.await_args.kwargs["stamp"]["pending_modification"]["reason"] == "scalp_time_stop"
    # non-scalp engines are untouched by the new step
    src = open(os.path.join(os.path.dirname(tm.__file__), "trade_manager.py"), encoding="utf-8").read()
    assert 'if trade.get("engine") in SCALP_ENGINES:' in src and tm.SCALP_BE_TRIGGER_R == 0.5 and tm.SCALP_TIME_STOP_MIN == 90
