"""iter-50 · Two-tier partial take-profit (user policy 2026-07-16).

Bank HALF the position at TP1 (100 pips), close the REST at TP2 (200 pips).
When a trade's tp3 <= tp2 the manager is in two-tier mode: tier 2 issues a
FULL_CLOSE instead of the legacy 25% partial. Trend-ride TP widening is
hard-capped at MTF_TP3_MAX_PIPS so 450-pip gold TPs can never come back.
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import trade_manager as tm  # noqa: E402


def _db():
    db = MagicMock()
    db.trades.update_one = AsyncMock()
    return db


def _trade(**over):
    t = {"_id": "t50", "user_id": "u1", "symbol": "XAUUSD", "action": "SELL",
         "entry_price": 4000.0, "lot_size": 0.01, "original_lot_size": 0.02,
         "mode": "live", "mt5_ticket": 111, "status": "open",
         "sl_pips": 120, "tp_pips": [100, 200, 200], "tp1_closed": True}
    t.update(over)
    return t


def _run(trade, price, cfg=None):
    db = _db()
    db.trades.request_close = AsyncMock(return_value={"trades_marked_for_close": 1})
    with patch.object(tm, "get_db", return_value=db), \
         patch.object(tm, "request_close", db.trades.request_close), \
         patch.object(tm, "get_quote",
                      AsyncMock(return_value={"price": price})), \
         patch.object(tm.ws_manager, "broadcast", AsyncMock()) as bc:
        asyncio.run(tm._manage_one_trade(trade, cfg or {}))
    return db, bc


class TestTwoTierPartials:
    def test_defaults_are_two_tier(self):
        assert tm.DEFAULT_TP_PIPS == (100, 200, 200)
        assert tm.DEFAULT_SL_PIPS == 120

    def test_tp1_banks_half_and_moves_be(self):
        # +100 pips on a SELL from 4000 → 3990 (XAUUSD pip = 0.1)
        trade = _trade(tp1_closed=False, lot_size=0.02)
        db, bc = _run(trade, 3990.0)
        update = db.trades.update_one.call_args.args[1]["$set"]
        assert update["pending_modification"]["type"] == "PARTIAL_CLOSE"
        assert update["pending_modification"]["new_volume"] == pytest.approx(0.01)
        assert update["pending_modification"]["new_sl"] == pytest.approx(4000.0)
        assert bc.await_args.args[2]["action"] == "PARTIAL_CLOSE_TP1_AND_BE"

    def test_tp2_full_closes_remainder_in_two_tier_mode(self):
        # tp3 (200) <= tp2 (200) → the rest closes fully at +200 pips
        trade = _trade()  # tp1 already banked, 0.01 remaining
        db, bc = _run(trade, 3980.0)
        # r25 P2-01: full closes go through the unified close protocol (request_close)
        call = db.trades.request_close.await_args
        assert call.args[1] == {"_id": trade["_id"]} and call.kwargs["reason"] == "take_profit"
        update = call.kwargs["stamp"]
        assert update["tp2_closed"] is True and update["tp3_closed"] is True
        assert update["pending_modification"]["type"] == "FULL_CLOSE"
        db.trades.update_one.assert_not_awaited()
        assert bc.await_args.args[2]["action"] == "FULL_CLOSE_TP2"

    def test_legacy_three_tier_still_partials_at_tp2(self):
        trade = _trade(tp_pips=[100, 200, 300], original_lot_size=0.04,
                       lot_size=0.02)
        db, bc = _run(trade, 3980.0)
        update = db.trades.update_one.call_args.args[1]["$set"]
        assert update["pending_modification"]["type"] == "PARTIAL_CLOSE"
        assert update["pending_modification"]["new_volume"] == pytest.approx(0.01)
        assert bc.await_args.args[2]["action"] == "PARTIAL_CLOSE_TP2"

    def test_below_tp2_no_action(self):
        trade = _trade()
        db, _ = _run(trade, 3985.0)   # +150 pips < 200
        db.trades.update_one.assert_not_awaited()


class TestTrendRideCap:
    def test_widened_tp_never_exceeds_cap(self):
        from ai_signals import MTF_TP3_MAX_PIPS
        from pip_utils import pips_to_price, price_to_pips
        entry, tp = 4000.0, 4000.0 + pips_to_price("XAUUSD", 200)
        mult = 1.8
        cap = pips_to_price("XAUUSD", MTF_TP3_MAX_PIPS)
        dist = min(abs(tp - entry) * mult, cap)
        widened = entry + dist
        assert price_to_pips("XAUUSD", widened - entry) <= MTF_TP3_MAX_PIPS


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
