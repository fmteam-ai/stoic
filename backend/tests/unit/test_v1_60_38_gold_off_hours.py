"""v1.60.38 — gold off-hours window: per-account UTC config (default 21:00–00:30), weekends blocked, spread guard
kept; deferred signals get a shadow outcome; Strategy Report shows Asian hours (00:30–06:00 UTC) as its own band."""
import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _t(h, m=0, day=14):      # 2026-10-14 is a Wednesday
    return datetime(2026, 10, day, h, m, tzinfo=timezone.utc)


def test_default_window_is_rollover_21_00_to_00_30_and_asian_hours_are_open():
    from agents.execution_optimizer_agent import _is_xau_off_hours, window_label
    assert _is_xau_off_hours(_t(21, 0)) and _is_xau_off_hours(_t(23, 59)) and _is_xau_off_hours(_t(0, 29))
    assert not _is_xau_off_hours(_t(0, 30)) and not _is_xau_off_hours(_t(3, 0)) and not _is_xau_off_hours(_t(5, 59))
    assert not _is_xau_off_hours(_t(20, 59)) and not _is_xau_off_hours(_t(12, 0))
    assert _is_xau_off_hours(_t(12, 0, day=17)) and _is_xau_off_hours(_t(12, 0, day=18))     # Sat / Sun always
    assert window_label({}) == "21:00–00:30 UTC"


def test_window_is_configurable_per_account_and_can_be_disabled():
    from agents.execution_optimizer_agent import _is_xau_off_hours, off_hours_window, window_label
    cfg = {"gold_off_hours_start_utc": "22:00", "gold_off_hours_end_utc": "06:00"}
    assert _is_xau_off_hours(_t(3, 0), cfg) and not _is_xau_off_hours(_t(21, 30), cfg)
    assert window_label(cfg) == "22:00–06:00 UTC"
    assert off_hours_window({"gold_off_hours_enabled": False}) is None
    assert not _is_xau_off_hours(_t(23, 0), {"gold_off_hours_enabled": False})
    assert _is_xau_off_hours(_t(12, 0, day=17), {"gold_off_hours_enabled": False})        # weekend still blocked
    assert off_hours_window({"gold_off_hours_start_utc": "garbage"}) == (21 * 60, 30)       # malformed → default
    m = _read("backend", "models.py")
    assert 'gold_off_hours_start_utc: str = "21:00"' in m and 'gold_off_hours_end_utc: str = "00:30"' in m
    assert "cfg=user_cfg" in _read("backend", "agents", "orchestrator.py")
    bc = _read("frontend", "src", "pages", "BotConfig.jsx")
    assert 'data-testid="gold-off-hours-window"' in bc and "gold_off_hours_start_utc: cfg.gold_off_hours_start_utc" in bc


def test_spread_guard_still_defers_in_asian_hours():
    from agents.execution_optimizer_agent import ExecutionOptimizerAgent
    agent = ExecutionOptimizerAgent()
    acct = {"current_spreads": {"XAUUSD": 9.0}, "median_spreads": {"XAUUSD": 3.0}}
    import agents.execution_optimizer_agent as eo
    real = eo._is_xau_off_hours
    eo._is_xau_off_hours = lambda now=None, cfg=None: real(_t(3, 0), cfg)
    try:
        out = _run(agent.optimize(signal={"action": "SELL", "symbol": "XAUUSD", "lot_size": 0.1}, account=acct, cfg={}))
        assert out["approved"] is False and "Spread 9.00 > 2.5×" in out["deferred_reason"] and out["session"] == "active"
        ok = _run(agent.optimize(signal={"action": "SELL", "symbol": "XAUUSD", "lot_size": 0.1},
                                 account={"current_spreads": {"XAUUSD": 3.2}, "median_spreads": {"XAUUSD": 3.0}}, cfg={}))
        assert ok["approved"] is True
        eo._is_xau_off_hours = lambda now=None, cfg=None: real(_t(22, 0), cfg)
        night = _run(agent.optimize(signal={"action": "SELL", "symbol": "XAUUSD", "lot_size": 0.1}, account={}, cfg={}))
        assert night["approved"] is False and "21:00–00:30 UTC" in night["deferred_reason"] and night["session"] == "off_hours"
    finally:
        eo._is_xau_off_hours = real


def _bar(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c}


def test_deferred_signals_get_a_shadow_outcome():
    from deferred_shadow import resolve_deferred_outcomes, utc_band
    db = FakeDb()
    ts = _t(22, 30)
    base = int(ts.timestamp())
    db.trade_decisions.rows += [
        {"_id": ObjectId(), "user_id": "u1", "status": "deferred", "symbol": "XAUUSD", "ts": ts.isoformat(),
         "signal": {"action": "SELL", "entry_price": 4080.0, "stop_loss": 4087.0, "take_profit": 4066.0, "scope": "hf_scalp"}},
        {"_id": ObjectId(), "user_id": "u1", "status": "deferred", "symbol": "XAUUSD", "ts": _t(3, 0, day=15).isoformat(),
         "signal": {"action": "BUY", "entry_price": 4080.0, "stop_loss": 4073.0, "take_profit": 4094.0, "scope": "hf_scalp"}},
        {"_id": ObjectId(), "user_id": "u1", "status": "rejected", "symbol": "XAUUSD", "ts": ts.isoformat(),
         "signal": {"action": "SELL", "entry_price": 4080.0, "stop_loss": 4087.0}},
    ]
    bars = [_bar(base + 900 * i, 4080, 4082, 4078, 4080) for i in range(1, 4)] + [_bar(base + 3600, 4079, 4080, 4065, 4066)]
    db.intraday_candles.rows.append({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M15", "bars": bars})
    out = _run(resolve_deferred_outcomes(db, "u1"))
    assert out["resolved"] == 1 and out["pending"] == 1                    # the 03:00 BUY has no bars after it yet
    sell = next(r for r in db.trade_decisions.rows if r["signal"]["action"] == "SELL" and r["status"] == "deferred")
    assert sell["shadow"]["outcome"] == "tp_first" and sell["shadow"]["r"] == 2.0 and sell["shadow"]["utc_band"] == "Rollover (21:00–00:30 UTC)"
    assert "shadow" not in next(r for r in db.trade_decisions.rows if r["status"] == "rejected")
    assert utc_band(_t(3, 0)) == "Asian hours (00:30–06:00 UTC)" and utc_band(_t(0, 29)) == "Rollover (21:00–00:30 UTC)"
    assert utc_band(_t(9, 0)) == "London (06:00–13:00 UTC)" and utc_band(_t(12, 0, day=17)) == "Weekend"
    assert "sweep_deferred_shadows" in _read("backend", "background_loops.py")


def test_strategy_report_has_asian_hours_band_and_deferred_shadow_rows():
    from strategy_report import build_report
    dec = [{"engine": "hf_scalp", "symbol": "XAUUSD", "pnl": 10.0, "win": True, "mirrored_accounts": 1, "mc_p": None,
            "range_pos_pct": None, "trend_ride": False, "closed_at": None, "utc_band": "Asian hours (00:30–06:00 UTC)"},
           {"engine": "hf_scalp", "symbol": "XAUUSD", "pnl": -5.0, "win": False, "mirrored_accounts": 1, "mc_p": None,
            "range_pos_pct": None, "trend_ride": False, "closed_at": None, "utc_band": "Asian hours (00:30–06:00 UTC)"},
           {"engine": "hf_scalp", "symbol": "XAUUSD", "pnl": 8.0, "win": True, "mirrored_accounts": 1, "mc_p": None,
            "range_pos_pct": None, "trend_ride": False, "closed_at": None, "utc_band": "London (06:00–13:00 UTC)"}]
    deferred = [{"ts": _t(22, 0).isoformat(), "signal": {"scope": "hf_scalp"}, "shadow": {"outcome": "tp_first", "r": 2.0, "utc_band": "Rollover (21:00–00:30 UTC)"}},
                {"ts": _t(23, 0).isoformat(), "signal": {"scope": "hf_scalp"}, "shadow": {"outcome": "sl_first", "r": -1.0, "utc_band": "Rollover (21:00–00:30 UTC)"}},
                {"ts": _t(23, 30).isoformat(), "signal": {"scope": "hf_scalp"}},                                   # not labelled yet
                {"ts": _t(3, 0).isoformat(), "signal": {"scope": "hf_scalp"}, "shadow": {"outcome": "tp_first", "r": 1.5, "utc_band": "Asian hours (00:30–06:00 UTC)"}}]
    rep = build_report(dec, 30, deferred)
    eng = rep["engines"][0]
    sess = {r["band"]: r for r in eng["sessions"]}
    assert list(sess) == ["Asian hours (00:30–06:00 UTC)", "London (06:00–13:00 UTC)"]
    assert sess["Asian hours (00:30–06:00 UTC)"]["n"] == 2 and sess["Asian hours (00:30–06:00 UTC)"]["win_rate"] == 50.0
    dsh = {r["band"]: r for r in eng["deferred_shadow"]}
    roll = dsh["Rollover (21:00–00:30 UTC)"]
    assert roll["n"] == 3 and roll["labelled"] == 2 and roll["tp_first"] == 1 and roll["win_rate"] == 50.0 and roll["avg_r"] == 0.5
    assert dsh["Asian hours (00:30–06:00 UTC)"]["win_rate"] == 100.0 and rep["deferred"] == 4
    fe = _read("frontend", "src", "components", "StrategyReport.jsx")
    assert "ENTRY SESSION (UTC)" in fe and "DEFERRED · SHADOW OUTCOME" in fe
