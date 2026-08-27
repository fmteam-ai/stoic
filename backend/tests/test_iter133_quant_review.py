"""iter-133 · Quant review phase-1 corrections (regression proofs).

1. RiskAgent / ExecutionOptimizer errors fail CLOSED (force HOLD).
2. Kelly disabled by default — fixed fractional risk until calibration.
3. Signal-time lot sizing removed — one account-aware stage in bot_runner.
4. Backtester retains pending orders for other symbols.
5. Backtester position ledger supports concurrent multi-symbol positions.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk import PROFILES, compute_lot_for_account, get_profile  # noqa: E402
from backtester.engine import (Engine as BacktestEngine, BarEvent,  # noqa: E402
                               EngineConfig, OrderEvent)

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)

ACCOUNT = {"equity": 10000.0, "account_type": "standard"}


def bar(sym, i, o, h, l, c):  # noqa: E741
    return BarEvent(ts=T0 + timedelta(minutes=15 * i), symbol=sym,
                    open=o, high=h, low=l, close=c)


class TestKellyDisabledByDefault:
    def test_default_is_fixed_fraction(self):
        p = get_profile("medium")  # risk_pct 1.0
        r = compute_lot_for_account(
            account=ACCOUNT, symbol="XAUUSD", entry_price=4000.0,
            stop_loss=3990.0, confidence_pct=70.0, profile=p)
        assert r["method"] == "fixed_fraction"
        assert r["effective_risk_pct"] == 1.0  # exactly profile risk, no Kelly scaling

    def test_kelly_optin_still_works(self):
        p = get_profile("medium")
        r = compute_lot_for_account(
            account=ACCOUNT, symbol="XAUUSD", entry_price=4000.0,
            stop_loss=3990.0, confidence_pct=66.0, profile=p, kelly_enabled=True)
        assert r["method"] == "kelly"
        assert r["effective_risk_pct"] < 1.0  # scaled down by kelly_f/cap

    def test_extreme_profile_no_full_kelly(self):
        assert PROFILES["extreme"]["kelly_cap"] <= 0.5  # H4

    def test_bot_runner_passes_flag(self):
        src = open(os.path.join(BACKEND, "bot_runner.py")).read()
        assert 'cfg.get("kelly_enabled", False)' in src
        assert "kelly_enabled=_kelly_on" in src


class TestSignalTimeSizingRemoved:
    def test_ai_signals_no_kelly_call(self):
        src = open(os.path.join(BACKEND, "ai_signals.py")).read()
        assert "compute_kelly_position_size" not in src
        assert "equity=1000.0" not in src
        assert '"sizing_deferred": True' in src


class TestFailClosed:
    def test_orchestrator_source_wiring(self):
        src = open(os.path.join(BACKEND, "agents", "orchestrator.py")).read()
        assert src.count('"forced_hold": True') == 2  # risk + exec optimizer
        assert "pipeline_safe_to_execute" in src
        assert '"conservative_fallback": True' in src  # allocator half-size

    def test_risk_agent_exception_forces_hold(self):
        # simulate the except-branch contract on a live signal dict
        import re
        src = open(os.path.join(BACKEND, "agents", "orchestrator.py")).read()
        risk_block = src.split("RiskAgent failed")[1][:600]
        assert 'signal["action"] = "HOLD"' in risk_block
        assert 'signal["tradeable"] = False' in risk_block
        eo_block = src.split("ExecutionOptimizer failed")[1][:700]
        assert 'signal["action"] = "HOLD"' in eo_block
        assert re.search(r'pipeline_safe_to_execute.*False', eo_block)


class TestBacktesterPendingRetention:
    def test_order_survives_other_symbol_bar(self):
        eng = BacktestEngine(EngineConfig(starting_equity=10000))
        # order for BTCUSD placed, then an XAUUSD bar arrives first
        eng.place_order(OrderEvent(ts=T0, symbol="BTCUSD", action="BUY",
                                   lot_size=0.1, stop_loss=None, take_profit=None))
        eng._settle_pending(bar("XAUUSD", 1, 4000, 4001, 3999, 4000))
        assert len(eng._pending_orders) == 1  # NOT cleared (old bug)
        eng._settle_pending(bar("BTCUSD", 2, 63000, 63100, 62900, 63050))
        assert not eng._pending_orders
        assert "BTCUSD" in eng.positions


class TestPositionLedger:
    def test_concurrent_positions_two_symbols(self):
        eng = BacktestEngine(EngineConfig(starting_equity=10000))
        eng.place_order(OrderEvent(ts=T0, symbol="XAUUSD", action="BUY",
                                   lot_size=1.0, stop_loss=None, take_profit=None))
        eng.place_order(OrderEvent(ts=T0, symbol="BTCUSD", action="SELL",
                                   lot_size=0.1, stop_loss=None, take_profit=None))
        eng._settle_pending(bar("XAUUSD", 1, 4000, 4001, 3999, 4000))
        eng._settle_pending(bar("BTCUSD", 1, 63000, 63100, 62900, 63000))
        assert set(eng.positions) == {"XAUUSD", "BTCUSD"}  # no overwrite (old bug)

    def test_close_targets_correct_symbol(self):
        eng = BacktestEngine(EngineConfig(starting_equity=10000))
        eng.place_order(OrderEvent(ts=T0, symbol="XAUUSD", action="BUY",
                                   lot_size=1.0, stop_loss=None, take_profit=None))
        eng.place_order(OrderEvent(ts=T0, symbol="BTCUSD", action="BUY",
                                   lot_size=0.1, stop_loss=None, take_profit=None))
        eng._settle_pending(bar("XAUUSD", 1, 4000, 4001, 3999, 4000))
        eng._settle_pending(bar("BTCUSD", 1, 63000, 63100, 62900, 63000))
        eng.place_order(OrderEvent(ts=T0, symbol="XAUUSD", action="CLOSE",
                                   lot_size=1.0, stop_loss=None, take_profit=None))
        eng._settle_pending(bar("XAUUSD", 2, 4010, 4011, 4009, 4010))
        assert "XAUUSD" not in eng.positions
        assert "BTCUSD" in eng.positions  # untouched

    def test_stops_only_fire_on_own_symbol_bar(self):
        eng = BacktestEngine(EngineConfig(starting_equity=10000))
        eng.place_order(OrderEvent(ts=T0, symbol="XAUUSD", action="BUY",
                                   lot_size=1.0, stop_loss=3990.0, take_profit=None))
        eng._settle_pending(bar("XAUUSD", 1, 4000, 4001, 3999, 4000))
        # a BTC bar with a huge low must not stop out the gold position
        eng._check_stops(bar("BTCUSD", 2, 100, 101, 1, 100))
        assert "XAUUSD" in eng.positions
        eng._check_stops(bar("XAUUSD", 3, 3995, 3996, 3985, 3990))
        assert "XAUUSD" not in eng.positions  # SL correctly hit

    def test_full_run_multi_symbol(self):
        eng = BacktestEngine(EngineConfig(starting_equity=10000))
        placed = {"done": False}

        def strat(b, macros, engine):
            if not placed["done"]:
                placed["done"] = True
                return [OrderEvent(ts=b.ts, symbol="XAUUSD", action="BUY",
                                   lot_size=1.0, stop_loss=None, take_profit=4010.0),
                        OrderEvent(ts=b.ts, symbol="BTCUSD", action="BUY",
                                   lot_size=0.1, stop_loss=None, take_profit=None)]
            return []

        bars = [bar("XAUUSD", 0, 4000, 4001, 3999, 4000),
                bar("BTCUSD", 1, 63000, 63010, 62990, 63000),
                bar("XAUUSD", 2, 4005, 4012, 4004, 4011),
                bar("BTCUSD", 3, 63050, 63060, 63040, 63055)]
        res = eng.run(bars, strat)
        # H1: gold TP hit intra-test; BTC settles at end-of-test (EOD policy)
        assert res.total_trades == 2
        assert any("EOD_SETTLEMENT" in f.note for f in res.fills
                   if f.action == "CLOSE")
        assert not eng.positions  # H1: nothing left unrealized after run()


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
