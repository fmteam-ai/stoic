"""v1.60.63 #85 — trade cost ledger + smart spread gate (shadow first)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

import trade_costs as TC  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


def _gold(action="BUY", **extra):
    entry, sl, tp = (4100.0, 4095.0, 4110.0) if action == "BUY" else (4100.0, 4105.0, 4090.0)
    return {"_id": "t1", "symbol": "XAUUSD", "action": action, "entry_price": entry, "stop_loss": sl, "take_profit": tp,
            "original_stop_loss": sl, "original_take_profit": tp, "lot_size": 0.10, "close_reason": "broker_confirmed",
            "spread_at_entry_pips": 2.5, "slippage_pips": 0.4, "commission_pips": 0.6, "pip_value_usd_per_lot": 10.0, **extra}


# ── A1 exit slippage: sign, BUY/SELL, SL/TP, BE-moved stop, gap-through ──────────────────────────────────────
def test_exit_slippage_sign_for_buy_and_sell_sl_and_tp_fills():
    assert TC.exit_slippage_pips(_gold("BUY"), 4094.7) == (3.0, "stop_loss")          # BUY stop filled 0.30 below → +3 pips worse
    assert TC.exit_slippage_pips(_gold("BUY"), 4095.1) == (-1.0, "stop_loss")         # filled better than the stop
    assert TC.exit_slippage_pips(_gold("BUY"), 4109.8) == (2.0, "take_profit")        # TP filled 0.20 short → worse
    assert TC.exit_slippage_pips(_gold("SELL"), 4105.3) == (3.0, "stop_loss")         # SELL stop filled 0.30 above → worse
    assert TC.exit_slippage_pips(_gold("SELL"), 4090.2) == (2.0, "take_profit")
    assert TC.exit_slippage_pips(_gold("SELL"), 4089.9) == (-1.0, "take_profit")
    be = _gold("BUY", stop_loss=4100.1)                                                # BE-moved stop: live level counts
    assert TC.exit_slippage_pips(be, 4099.9) == (2.0, "stop_loss")
    assert TC.exit_slippage_pips(_gold("BUY"), 4092.0) == (30.0, "stop_loss")         # gapped through the stop
    assert TC.exit_slippage_pips(_gold("BUY"), 4103.0) == (None, None)                # manual mid-way close: neither
    assert TC.exit_slippage_pips(_gold("BUY"), None) == (None, None)


def test_exit_slippage_uses_per_symbol_pip_sizes():
    fx = {"symbol": "EURUSD", "action": "BUY", "entry_price": 1.0850, "stop_loss": 1.0840, "take_profit": 1.0870, "close_reason": "broker_confirmed"}
    assert TC.exit_slippage_pips(fx, 1.08385) == (1.5, "stop_loss")
    idx = {"symbol": "US30", "action": "SELL", "entry_price": 42000.0, "stop_loss": 42050.0, "take_profit": 41900.0, "close_reason": "broker_confirmed"}
    assert TC.exit_slippage_pips(idx, 42053.0) == (3.0, "stop_loss")
    btc = {"symbol": "BTCUSD", "action": "BUY", "entry_price": 65000.0, "stop_loss": 64800.0, "take_profit": 65400.0, "close_reason": "broker_confirmed"}
    assert TC.exit_slippage_pips(btc, 64785.0) == (15.0, "stop_loss")
    assert TC.cost_summary(btc, 20.0, 64785.0)["pip_size"] == 1.0 and TC.cost_summary(fx, 0.8, 1.08385)["pip_size"] == 0.0001


# ── A2 cost summary math ─────────────────────────────────────────────────────────────────────────────────────
def test_cost_summary_in_pips_usd_R_and_pct_of_tp():
    cs = TC.cost_summary(_gold("BUY"), exit_spread_pips=3.1, exit_price=4094.7)
    assert cs["pips"] == {"spread_entry": 2.5, "slip_entry": 0.4, "slip_exit": 3.0, "commission": 0.6}
    assert cs["total_cost_pips"] == 6.5
    assert cs["usd"]["spread_entry"] == 2.5 and cs["total_cost_usd"] == 6.5                 # 0.10 lot × $10/pip/lot
    assert cs["total_cost_R"] == pytest.approx(6.5 / 50.0)                                   # original stop 50 pips
    assert cs["cost_pct_of_tp"] == pytest.approx(6.5)                                         # original TP 100 pips
    assert cs["exit_kind"] == "stop_loss" and cs["exit_spread_pips"] == 3.1 and cs["missing"] == []
    # BE-moved stop: R and % still use the ORIGINAL distances
    cs2 = TC.cost_summary(_gold("BUY", stop_loss=4100.1), None, 4099.9)
    assert cs2["total_cost_R"] == pytest.approx((2.5 + 0.4 + 2.0 + 0.6) / 50.0)
    sparse = TC.cost_summary({"symbol": "XAUUSD", "action": "BUY", "entry_price": 4100.0, "stop_loss": 4095.0, "take_profit": 4110.0,
                              "lot_size": 0.1, "close_reason": "manual"}, None, 4103.0)
    assert sorted(sparse["missing"]) == ["commission", "slip_entry", "slip_exit", "spread_entry"] and sparse["total_cost_pips"] == 0


def test_stamp_exit_costs_writes_the_ledger_fields():
    db = FakeDb()
    tr = dict(_gold("BUY"), _id="abc", exit_price=4094.7)
    db.trades.rows.append(tr)
    acc = {"_id": "a1", "current_spreads": {"XAUUSD": 3.3}}
    upd = _run(TC.stamp_exit_costs(db, tr, acc, 4094.7))
    row = db.trades.rows[0]
    assert upd["exit_slippage_pips"] == 3.0 and row["exit_kind"] == "stop_loss" and row["exit_spread_pips"] == 3.3
    assert row["cost_summary"]["total_cost_R"] == pytest.approx(0.13)
    assert _run(TC.stamp_exit_costs(db, dict(tr, exit_price=None), acc, None)) is None
    src = open(os.path.join(ROOT, "routes", "bridge_routes.py"), encoding="utf-8").read()
    body = src.split("async def external_deal(")[1]
    assert "from trade_costs import stamp_exit_costs" in body and body.index("stamp_exit_costs") > body.index('await db.trades.update_one({"_id": existing["_id"]}, {"$set": update})')


# ── A3 hour-of-day spread history ────────────────────────────────────────────────────────────────────────────
def test_spread_history_samples_per_hour_and_median_fallback_below_30():
    db = FakeDb()
    now = datetime(2026, 10, 20, 9, 15, tzinfo=timezone.utc)
    for i in range(12):
        _run(TC.record_spread_samples(db, "a1", {"XAUUSD.fx": 2.0 + i * 0.1, "EURUSD": 0.6}, now + timedelta(minutes=i)))
    assert len(db.spread_history.rows) == 2
    gold = next(r for r in db.spread_history.rows if r["symbol"] == "XAUUSD")
    # v1.60.64 #86 — one sample per UTC minute under minutes.<hour>.<mm> (was a pushed list of the newest 60)
    assert gold["day"] == "2026-10-20" and len(gold["minutes"]["9"]) == 12 and gold["expires_at"] > now + timedelta(days=14)
    st = TC.hour_stats_from_docs([gold], 9)
    assert st["n"] == 12 and st["median"] == 2.55 and st["p90"] == 3.0
    assert TC.hour_stats_from_docs([gold], 10) == {"n": 0, "median": None, "p90": None}
    cfg = {"spread_gate_mult": 1.5}
    # < 30 samples → the hour median is ignored and the static cap is the reference
    d = TC.spread_gate_decision(cfg, symbol="XAUUSD", spread_now=4.1, hour=st, static_cap=50.0, tp_pips=None, median_slip_entry=None, commission_pips=None)
    assert d["basis"] == "static_cap" and d["would_block"] is False
    d2 = TC.spread_gate_decision(cfg, symbol="XAUUSD", spread_now=55.0, hour=st, static_cap=50.0, tp_pips=None, median_slip_entry=None, commission_pips=None)
    assert d2["would_block"] and d2["rule"] == "spread_vs_static_cap" and "needs 30 samples, have 12" in d2["reason"]
    assert TC.spread_gate_decision(cfg, symbol="XAUUSD", spread_now=4.1, hour=st, static_cap=None, tp_pips=None, median_slip_entry=None, commission_pips=None)["basis"] == "no_reference"


# ── B6 gate rules ────────────────────────────────────────────────────────────────────────────────────────────
def test_gate_blocks_on_hour_median_and_on_cost_pct_of_tp():
    cfg = {"spread_gate_mult": 1.5, "max_cost_pct_of_tp": 12}
    hour = {"n": 40, "median": 2.4, "p90": 3.5}
    d = TC.spread_gate_decision(cfg, symbol="XAUUSD", spread_now=4.1, hour=hour, static_cap=50.0, tp_pips=100.0, median_slip_entry=0.4, commission_pips=0.6)
    assert d["would_block"] and d["rule"] == "spread_vs_hour_median" and d["reason"] == "Spread gate — XAUUSD spread 4.1p > 1.5× hour median 2.4p"
    assert d["basis"] == "hour_median" and d["cost_pct"] == pytest.approx(5.1)
    ok = TC.spread_gate_decision(cfg, symbol="XAUUSD", spread_now=3.5, hour=hour, static_cap=50.0, tp_pips=100.0, median_slip_entry=0.4, commission_pips=0.6)
    assert ok["would_block"] is False and ok["reason"] is None
    # (b) cost rule: spread 3.0 + slip 0.4 + comm 0.6 = 4.0p on a 30-pip TP = 13.3 % > 12 %
    cost = TC.spread_gate_decision(cfg, symbol="XAUUSD", spread_now=3.0, hour=hour, static_cap=50.0, tp_pips=30.0, median_slip_entry=0.4, commission_pips=0.6)
    assert cost["would_block"] and cost["rule"] == "cost_pct_of_tp" and cost["cost_pct"] == pytest.approx(13.33, abs=0.01)
    assert "13% of TP 30.0p (max 12%)" in cost["reason"]
    assert TC.spread_gate_decision(cfg, symbol="XAUUSD", spread_now=None, hour=hour, static_cap=50.0, tp_pips=30.0, median_slip_entry=0.4, commission_pips=0.6)["basis"] == "no spread"
    assert TC.gate_mode({}) == "shadow" and TC.gate_mode({"spread_gate_mode": "ENFORCE"}) == "enforce" and TC.gate_mode({"spread_gate_mode": "weird"}) == "shadow"


def test_runner_shadow_never_blocks_and_enforce_skips_with_pulse():
    src = open(os.path.join(ROOT, "bot_runner.py"), encoding="utf-8").read()
    block = src.split("# v1.60.63 #85 — smart spread gate")[1].split("# v1.60.48/50 — per-USER cross-account exposure cap")[0]
    assert 'if _sg_mode(cfg) != "off"' in block and 'signal["spread_gate"] = _sg' in block
    assert 'if _sg["mode"] == "enforce":' in block and "continue" in block.split('if _sg["mode"] == "enforce":')[1]
    assert block.index('if _sg["mode"] == "enforce":') > block.index('if _sg.get("would_block"):')      # shadow: counted only
    assert 'reason=_sg["reason"]' in block and 'action="SKIP"' in block
    assert "fail-open" in block.lower()
    assert src.index("# v1.60.63 #85 — smart spread gate") > src.index("from execution_timing import consider_delay")   # after existing filters
    assert src.index("# v1.60.63 #85 — smart spread gate") < src.index("trade_doc = await engine.execute(")           # before dispatch
    models = open(os.path.join(ROOT, "models.py"), encoding="utf-8").read()
    assert 'spread_gate_mode: str = "shadow"' in models and "spread_gate_mult: float = 1.5" in models and "max_cost_pct_of_tp: float = 12.0" in models


# ── A4 / B7 report + scoreboard ──────────────────────────────────────────────────────────────────────────────
def test_summarise_costs_sessions_worst_and_shadow_scoreboard():
    def t(hour, pnl, risk, cost_r, gate_block, **extra):
        return {"_id": f"t{hour}{pnl}", "symbol": "XAUUSD", "action": "BUY", "pnl": pnl, "risk_amount": risk,
                "created_at": f"2026-10-15T{hour:02d}:10:00+00:00", "spread_at_entry_pips": 2.0 + hour / 10, "slippage_pips": 0.3, "exit_slippage_pips": 1.0,
                "cost_summary": {"total_cost_R": cost_r, "cost_pct_of_tp": cost_r * 100, "total_cost_usd": cost_r * 25, "exit_kind": "stop_loss"},
                "spread_gate": {"would_block": gate_block, "rule": "spread_vs_hour_median" if gate_block else None}, **extra}
    rows = [t(2, -25, 25, 0.08, True), t(9, 50, 25, 0.05, False), t(14, -25, 25, 0.12, True), t(15, 40, 25, 0.04, False), t(22, 10, 25, 0.06, False)]
    out = TC.summarise_costs(rows)
    assert out["trades"] == 5 and out["with_costs"] == 5 and out["avg_total_cost_R"] == pytest.approx(0.07)
    assert set(out["by_session"]) == {"asia", "london", "ny", "late"} and out["by_session"]["ny"]["trades"] == 2
    assert [w["total_cost_R"] for w in out["worst_trades"]] == [0.12, 0.08, 0.06, 0.05, 0.04]
    sb = out["shadow_scoreboard"]
    assert sb["would_block"] == {"trades": 2, "win_rate": 0.0, "net_R": -2.0, "net_usd": -50.0}
    assert sb["passed"] == {"trades": 3, "win_rate": 100.0, "net_R": 4.0, "net_usd": 100.0}
    assert sb["rules"]["spread_vs_hour_median"] == 2
    assert TC.session_of(0) == "asia" and TC.session_of(7) == "london" and TC.session_of(13) == "ny" and TC.session_of(23) == "late"
    diag = open(os.path.join(ROOT, "routes", "diagnostic_routes.py"), encoding="utf-8").read()
    assert '@router.get("/trade-costs")' in diag and "report(db, user[\"id\"], days)" in diag
    ins = open(os.path.join(ROOT, "routes", "insights_routes.py"), encoding="utf-8").read()
    assert 'payload["trading_costs"]' in ins
    jsx = open(os.path.join(ROOT, "..", "frontend", "src", "pages", "BotConfig.jsx"), encoding="utf-8").read()
    assert "spread-cap-usd-" in jsx and 'data-testid="spread-gate-mode-select"' in jsx
