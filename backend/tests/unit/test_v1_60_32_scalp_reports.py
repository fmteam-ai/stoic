"""v1.60.32 — engine persisted at open · Exit Rule Scoreboard · Shadow Expectancy Report."""
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
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _src(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


# ── engine persisted at open ───────────────────────────────────────────────────────────────────────────────────
def test_engine_for_signal_falls_back_to_a_known_scope_and_execution_uses_it():
    from strategy_engines import engine_for_signal
    assert engine_for_signal({"engine": None, "scope": "hf_scalp_fast"}) == "hf_scalp_fast"
    assert engine_for_signal({"engine": "breakout_m15", "scope": "hf_scalp"}) == "breakout_m15"
    assert engine_for_signal({"engine": None, "scope": "scalp_fast"}) is None        # Scalp Fast Path is not an engine
    assert engine_for_signal({}) is None
    src = _src("backend", "execution.py")
    assert src.count('"engine": engine_for_signal(signal)') == 2 and '"engine": signal.get("engine")' not in src
    assert "backfill_trade_engines" in _src("backend", "seed.py")


# ── exit rule scoreboard ───────────────────────────────────────────────────────────────────────────────────────
def _closed(**kw):
    t = {"_id": ObjectId(), "user_id": "u1", "status": "closed", "symbol": "XAUUSD", "engine": None, "scope": "hf_scalp_fast",
         "lot_size": 0.05, "original_lot_size": 0.05, "sl_pips": 400, "pnl": -17.44, "close_reason": "stop_loss",
         "closed_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}
    t.update(kw)
    return t


def test_exit_rule_scoreboard_counts_fired_rules_and_estimates_spared_loss():
    from scalp.reports import classify_exit, exit_rule_scoreboard, planned_risk_usd
    be_stop = _closed(scalp_exit_rule="breakeven_0_5r", pnl=0.9, close_reason="stop_loss", risk_amount=20.0)
    be_target = _closed(scalp_exit_rule="breakeven_0_5r", pnl=38.0, close_reason="take_profit", risk_amount=20.0)
    ts = _closed(scalp_exit_rule="time_stop_90m", pnl=-6.0, close_reason="time_stop", risk_amount=20.0)
    plain = _closed()
    other_engine = _closed(scope="breakout_m15", pnl=-50.0)
    old = _closed(closed_at=(datetime.now(timezone.utc) - timedelta(days=40)).isoformat(), scalp_exit_rule="time_stop_90m")
    assert planned_risk_usd(be_stop) == 20.0 and planned_risk_usd(plain) is not None
    assert classify_exit(be_stop)["outcome"] == "be_stop" and classify_exit(be_stop)["avoided_loss"] == 20.0
    assert classify_exit(be_target)["outcome"] == "target" and classify_exit(be_target)["avoided_loss"] is None
    assert classify_exit(ts)["outcome"] == "time_stop" and classify_exit(ts)["avoided_loss"] == 14.0
    db = FakeDb()
    db.trades.rows += [be_stop, be_target, ts, plain, other_engine, old]
    out = _run(exit_rule_scoreboard(db, "u1", days=30))
    be, tsg = out["rules"]["breakeven_0_5r"], out["rules"]["time_stop_90m"]
    assert be["trades"] == 2 and be["be_stop"] == 1 and be["target"] == 1 and be["pnl_usd"] == 38.9 and be["avoided_loss_usd"] == 20.0
    assert tsg["trades"] == 1 and tsg["closed_negative"] == 1 and tsg["avoided_loss_usd"] == 14.0
    assert out["unaffected"]["trades"] == 1 and out["fired"] == 3 and out["avoided_loss_usd"] == 34.0
    assert "spared an estimated $34.00" in out["lesson"]
    assert _run(exit_rule_scoreboard(FakeDb(), "u1", days=30))["lesson"].startswith("Neither exit rule has fired")


# ── shadow expectancy report ───────────────────────────────────────────────────────────────────────────────────
def test_shadow_expectancy_report_per_symbol_with_gate_and_live_stats(monkeypatch):
    from scalp import reports
    db = FakeDb()
    base = {"user_id": "u1", "setup": {"setup": "range_break_momentum_v2"}}
    for v in (6.0, -4.0, 5.0):
        db.scalp_decisions.rows.append({"_id": ObjectId(), **base, "symbol": "XAUUSD", "execution_mode": "shadow", "outcome": {"result": "x", "net_pips": v}})
    for v in (8.0, -3.0):
        db.scalp_decisions.rows.append({"_id": ObjectId(), **base, "symbol": "XAUUSD", "execution_mode": "demo_live", "outcome": {"result": "x", "net_pips": v}})
    db.scalp_decisions.rows.append({"_id": ObjectId(), **base, "symbol": "XAUUSD", "execution_mode": "shadow"})   # unlabeled
    sh = _run(reports.mode_stats(db, "u1", "XAUUSD", "shadow"))
    lv = _run(reports.mode_stats(db, "u1", "XAUUSD", "demo_live"))
    assert sh == {"mode": "shadow", "labeled": 3, "net_pips": 7.0, "expectancy_pips": 2.333, "profit_factor": 2.75, "win_rate": 0.667}
    assert lv["labeled"] == 2 and lv["expectancy_pips"] == 2.5
    gate = AsyncMock(return_value={"eligible": False, "labeled": 3, "expectancy_pips": 2.333, "profit_factor": 2.75,
                                   "reasons": ["3/300 labeled shadow samples"], "thresholds": {"min_labeled": 300}})
    with patch("scalp.live_gate.live_eligibility", gate):
        out = _run(reports.shadow_expectancy_report(db, "u1", ["EURUSD", "XAUUSD"]))
    assert [r["symbol"] for r in out["symbols"]] == ["EURUSD", "XAUUSD"]
    x = out["symbols"][1]
    assert x["gate"]["eligible"] is False and x["shadow"]["labeled"] == 3 and x["live"]["labeled"] == 2
    assert out["symbols"][0]["shadow"]["labeled"] == 0
    routes = _src("backend", "routes", "scalp_routes.py")
    assert '@router.get("/exit-rules")' in routes and '@router.get("/expectancy")' in routes
    page = _src("frontend", "src", "pages", "Scalp.jsx")
    assert "<ShadowExpectancyReport />" in page and "<ExitRuleScoreboard days={30} />" in page
    comp = _src("frontend", "src", "components", "ScalpReports.jsx")
    assert "shadow-expectancy-report" in comp and "exit-rule-scoreboard" in comp and "/scalp/expectancy" in comp
