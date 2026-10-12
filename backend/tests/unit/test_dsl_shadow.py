"""Shadow runner for strategy DSLs — forward-only paper trading with exact catch-up."""
import asyncio
import math
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.unit

import dsl_engine as de  # noqa: E402
import dsl_shadow as ds  # noqa: E402
from fake_mongo import FakeDb  # noqa: E402

T0 = 1_760_000_000 - 1_760_000_000 % 900
DSL = {"version": "1.0", "name": "osc", "symbols": ["XAUUSD"], "params": {"rsi_oversold": 45, "rsi_overbought": 55},
       "entry_rules": [{"field": "rsi_14", "op": "<", "value_ref": "params.rsi_oversold", "side": "BUY"},
                       {"field": "rsi_14", "op": ">", "value_ref": "params.rsi_overbought", "side": "SELL"}],
       "exit_rules": [{"kind": "take_profit_pct", "value": 0.4}, {"kind": "stop_loss_pct", "value": 0.4},
                      {"kind": "trail_pct", "value": 0.3}],
       "pseudocode": ""}


def _raw(n, start=0):
    out = []
    for i in range(start, start + n):
        c0, c1 = 100 + 2 * math.sin(i / 5), 100 + 2 * math.sin((i + 1) / 5)
        out.append({"t": T0 + i * 900, "o": c0, "h": max(c0, c1) + 0.15, "l": min(c0, c1) - 0.15, "c": c1})
    return out


def _db(bars, t_basis="utc", user="u1"):
    db = FakeDb()
    db.intraday_candles.rows.append({"user_id": user, "symbol": "XAUUSD", "timeframe": "M15",
                                     "t_basis": t_basis, "bars": bars})
    return db


def _run(c):
    return asyncio.run(c)


def _strat(db):
    return _run(db.dsl_shadow_strategies.find_one({}))


def test_promote_is_forward_only_and_keeps_backtest_snapshot():
    db = _db(_raw(300))
    doc = _run(ds.promote(db, "u1", DSL, "XAUUSD.r"))
    assert doc["symbol"] == "XAUUSD" and doc["mode"] == "shadow" and doc["status"] == "active"
    assert doc["last_ts"] == T0 + 299 * 900
    assert doc["backtest"]["trades_n"] > 0 and "Buy at the next bar's open" in doc["explanation"]
    assert _run(ds.advance(db, _strat(db)))["processed"] == 0          # no new bars → nothing traded
    assert not db.dsl_shadow_trades.rows


def test_catch_up_in_one_go_equals_bar_by_bar():
    history, future = _raw(300), _raw(400, start=300)
    one = _db(list(history))
    _run(ds.promote(one, "u1", DSL, "XAUUSD"))
    many = _db(list(history))
    _run(ds.promote(many, "u1", DSL, "XAUUSD"))

    one.intraday_candles.rows[0]["bars"] = history + future            # a long outage, then catch-up
    _run(ds.advance(one, _strat(one)))
    for k in range(len(future)):                                       # every bar as it closes
        many.intraday_candles.rows[0]["bars"] = history + future[:k + 1]
        _run(ds.advance(many, _strat(many)))

    key = lambda t: (t["entry_ts"], t["exit_ts"], round(t["return_pct"], 9), t["reason"])  # noqa: E731
    a = sorted(map(key, one.dsl_shadow_trades.rows))
    b = sorted(map(key, many.dsl_shadow_trades.rows))
    assert a == b and len(a) > 5
    assert all(t[0] > T0 + 299 * 900 for t in a)                       # nothing before promotion
    assert _strat(one)["state"] == _strat(many)["state"]


def test_shadow_matches_the_backtester_step_for_step():
    bars = _raw(700)
    db = _db(bars[:300])
    _run(ds.promote(db, "u1", DSL, "XAUUSD"))
    db.intraday_candles.rows[0]["bars"] = bars
    _run(ds.advance(db, _strat(db)))
    nb = de.normalize_bars(bars)
    feats, ctx, state, expect = de.feature_series(nb, True), de.make_ctx(DSL), de.new_state(), []
    for i in range(300, 700):
        t = de.step(state, nb[i], feats[i], ctx)
        if t:
            expect.append(t)
    got = sorted(db.dsl_shadow_trades.rows, key=lambda t: t["entry_ts"])
    assert [(t["entry_ts"], t["exit"]) for t in got] == [(t["entry_ts"], t["exit"]) for t in expect]


def test_repeat_and_concurrent_advances_never_duplicate():
    db = _db(_raw(300))
    _run(ds.promote(db, "u1", DSL, "XAUUSD"))
    db.intraday_candles.rows[0]["bars"] = _raw(600)
    stale = _strat(db)                                                 # a second process read the same doc
    _run(ds.advance(db, _strat(db)))
    n = len(db.dsl_shadow_trades.rows)
    assert _run(ds.advance(db, stale))["skipped"] == "advanced concurrently"
    _run(ds.advance(db, _strat(db)))
    assert len(db.dsl_shadow_trades.rows) == n > 0


def test_rolled_window_counts_a_gap_and_non_utc_clock_is_not_advanced():
    db = _db(_raw(300))
    _run(ds.promote(db, "u1", DSL, "XAUUSD"))
    db.intraday_candles.rows[0]["bars"] = _raw(100, start=1000)       # last_ts fell out of the window
    r = _run(ds.advance(db, _strat(db)))
    assert r["gap"] and _strat(db)["gaps"] == 1

    db2 = _db(_raw(300), t_basis="broker_unknown")
    _run(ds.promote(db2, "u1", DSL, "XAUUSD"))
    db2.intraday_candles.rows[0]["bars"] = _raw(400)
    assert "UTC" in _run(ds.advance(db2, _strat(db2)))["skipped"]


def test_promotion_without_candles_anchors_later():
    db = FakeDb()
    doc = _run(ds.promote(db, "u1", DSL, "XAUUSD"))
    assert doc["last_ts"] is None and doc["backtest"] is None
    db.intraday_candles.rows.append({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M15",
                                     "t_basis": "utc", "bars": _raw(300)})
    _run(ds.advance(db, _strat(db)))
    assert _strat(db)["last_ts"] == T0 + 299 * 900 and not db.dsl_shadow_trades.rows


def test_limit_stop_and_ownership():
    db = _db(_raw(300))
    for _ in range(ds.MAX_ACTIVE_PER_USER):
        _run(ds.promote(db, "u1", DSL, "XAUUSD"))
    with pytest.raises(ValueError):
        _run(ds.promote(db, "u1", DSL, "XAUUSD"))
    sid = str(db.dsl_shadow_strategies.rows[0]["_id"])
    assert _run(ds.stop(db, "intruder", sid)) is False
    assert _run(ds.stop(db, "u1", "not-an-id")) is False
    assert _run(ds.stop(db, "u1", sid)) is True
    db.intraday_candles.rows[0]["bars"] = _raw(600)
    assert _run(ds.advance_all(db)) == ds.MAX_ACTIVE_PER_USER - 1      # the stopped one is skipped


def test_live_stats():
    s = ds.live_stats([{"return_pct": 1.0, "exit_ts": 2}, {"return_pct": -0.5, "exit_ts": 1}])
    assert s["trades_n"] == 2 and s["win_rate"] == 0.5
    assert s["total_return_pct"] == pytest.approx((1.01 * 0.995 - 1) * 100, abs=1e-3)


@pytest.mark.asyncio
async def test_shadow_routes(monkeypatch):
    from fastapi import FastAPI
    from auth import get_current_user
    from routes import nl_routes
    db = _db(_raw(300))
    monkeypatch.setattr(nl_routes, "get_db", lambda: db)
    app = FastAPI()
    app.include_router(nl_routes.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "role": "user"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/nl/strategy/shadow", json={"dsl": DSL, "name": "osc v1"})
        assert r.status_code == 200 and "state" not in r.json() and r.json()["name"] == "osc v1"
        sid = r.json()["id"]
        monkeypatch.setattr(ds, "MAX_ACTIVE_PER_USER", 1)
        r = await c.post("/nl/strategy/shadow", json={"dsl": DSL})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "shadow_limit_reached"
        monkeypatch.setattr(ds, "MAX_ACTIVE_PER_USER", 10)
        assert (await c.post("/nl/strategy/shadow", json={"dsl": DSL, "symbol": "BTCUSD"})).status_code == 422
        db.intraday_candles.rows[0]["bars"] = _raw(600)
        body = (await c.get("/nl/strategy/shadow")).json()               # listing catches up first
        s = body["strategies"][0]
        assert s["id"] == sid and "state" not in s and s["live"]["trades_n"] > 0 and s["backtest"]
        assert (await c.post(f"/nl/strategy/shadow/{sid}/stop")).status_code == 200
        assert (await c.post(f"/nl/strategy/shadow/{sid}/stop")).status_code == 404


def test_analytics_loop_runs_shadow_isolated():
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "background_loops.py"), encoding="utf-8").read()
    body = src[src.index("async def _analytics_loop"):src.index("async def _model_maintenance_loop")]
    hook = body[body.index("from dsl_shadow import advance_all") - 40:]
    assert hook.index("try:") < hook.index("advance_all(get_db())") < hook.index("except Exception")
    assert body.index('record_progress("_analytics_loop"') < body.index("advance_all(get_db())")
