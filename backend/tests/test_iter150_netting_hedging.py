"""iter-150 · Netting vs hedging automated scenario suite:
partial fills, merged positions, multiple deals, restart during execution.
EA scenarios are source-level (MQL5 cannot run here); backend scenarios run
live against /api/bridge/report with real MongoDB verification."""
import os as _os
import time
import asyncio
import requests
import pytest
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(_os.path.join(_BACKEND_DIR, ".env"))
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
from live_target import require_live_base_url

EA_PATH = _os.path.join(_BACKEND_DIR, "static", "EmergentTradingBridge.mq5")


def _ea():
    return open(EA_PATH).read()


def _body(src, start, end=None):
    i = src.index(start)
    j = src.index(end) if end else len(src)
    return src[i:j]


class TestEaSourceScenarios:
    """Netting/hedging behavior encoded in the EA source (CI-safe)."""

    def test_hedging_position_ticket_is_identifier(self):
        fn = _body(_ea(), "bool SelectPositionById(", "// Drop journal entries") \
            if "// Drop journal entries" in _ea() else _body(_ea(), "bool SelectPositionById(")
        fn = _body(_ea(), "bool SelectPositionById(")[:1200]
        assert "PositionSelectByTicket(position_id)" in fn
        assert "POSITION_IDENTIFIER" in fn

    def test_netting_fallback_is_margin_mode_gated(self):
        fn = _body(_ea(), "bool SelectPositionById(")[:1500]
        assert "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING" in fn
        assert "PositionSelect(symbol)" in fn
        # the symbol fallback must only run when netting
        assert fn.index("ACCOUNT_MARGIN_MODE_RETAIL_HEDGING") \
            < fn.index("PositionSelect(symbol)")

    def test_merged_position_never_pollutes_trade_fill(self):
        et = _body(_ea(), "void ExecuteTrade(", "void ApplyFullClose(")
        assert "OrderFilledVolume(res.order)" in et
        assert "filled = PositionGetDouble(POSITION_VOLUME);" not in et

    def test_multiple_deals_summed_per_order(self):
        fn = _body(_ea(), "double OrderFilledVolume(")[:900]
        assert "total += HistoryDealGetDouble(dtk, DEAL_VOLUME);" in fn
        assert "DEAL_ORDER" in fn

    def test_restart_during_execution_paths(self):
        et = _body(_ea(), "void ExecuteTrade(", "void ApplyFullClose(")
        # crash before result journaled → history recovery, no resend
        assert "if (jstate == JR_ORDER_SENT)" in et
        # accepted-unresolved restart → resolution retry, no resend
        assert "if (jstate == JR_ACCEPTED)" in et
        # already reported → replay only
        assert "ReportOpenFromJournal(trade_id)" in et


def _db():
    return AsyncIOMotorClient(_os.environ["MONGO_URL"])[_os.environ["DB_NAME"]]


async def _fixture(db):
    acc = await db.accounts.find_one(
        {"bridge_token": {"$exists": True, "$ne": None}})
    assert acc, "need a seeded account with bridge_token"
    return acc


def _report(payload):
    base = require_live_base_url()
    r = requests.post(f"{base}/api/bridge/report", json=payload, timeout=15)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.json()


async def _mk_trade(db, acc, lot):
    ins = await db.trades.insert_one({
        "account_id": str(acc["_id"]), "user_id": acc["user_id"],
        "symbol": "EURUSD", "action": "BUY", "lot_size": lot,
        "status": "pending", "origin": "auto", "marker": "iter150"})
    return ins


class TestBackendScenarios:
    """Live E2E: how the backend consumes broker truth in both modes."""

    def test_hedging_full_fill(self):
        async def run():
            db = _db()
            acc = await _fixture(db)
            ins = await _mk_trade(db, acc, 0.50)
            try:
                tid = str(ins.inserted_id)
                base = int(time.time() * 1000) % 10**9
                _report({"bridge_token": acc["bridge_token"], "trade_id": tid,
                         "mt5_ticket": base + 3, "status": "open",
                         "entry_price": 1.1, "order_ticket": base + 1,
                         "deal_ticket": base + 2, "position_id": base + 3,
                         "filled_volume": 0.50, "partial_fill": False,
                         "position_volume": 0.50})
                t = await db.trades.find_one({"_id": ins.inserted_id})
                assert t["status"] == "open" and t["lot_size"] == 0.50
                assert not t.get("partial_fill")
                assert t["position_id"] == base + 3
            finally:
                await db.trades.delete_one({"_id": ins.inserted_id})
        asyncio.run(run())

    def test_netting_merged_position_keeps_trade_attribution(self):
        """0.80 pre-existing + 0.20 STOIC → position 1.00 but the TRADE
        stays 0.20 (the v1.52 corruption case)."""
        async def run():
            db = _db()
            acc = await _fixture(db)
            ins = await _mk_trade(db, acc, 0.20)
            try:
                tid = str(ins.inserted_id)
                base = int(time.time() * 1000) % 10**9 + 1000
                _report({"bridge_token": acc["bridge_token"], "trade_id": tid,
                         "mt5_ticket": base + 3, "status": "open",
                         "entry_price": 1.1, "order_ticket": base + 1,
                         "deal_ticket": base + 2, "position_id": base + 3,
                         "filled_volume": 0.20, "partial_fill": False,
                         "position_volume": 1.00})
                t = await db.trades.find_one({"_id": ins.inserted_id})
                assert t["lot_size"] == 0.20, "netting merge must not inflate the trade"
                assert t["position_volume"] == 1.00
                assert not t.get("partial_fill")
                assert not t.get("original_lot_size")
            finally:
                await db.trades.delete_one({"_id": ins.inserted_id})
        asyncio.run(run())

    def test_multiple_deals_partial_fill_adopted(self):
        """Requested 1.00, broker filled 0.60 across several deals →
        partial adopted, original preserved, audit event written."""
        async def run():
            db = _db()
            acc = await _fixture(db)
            ins = await _mk_trade(db, acc, 1.00)
            tid = str(ins.inserted_id)
            try:
                base = int(time.time() * 1000) % 10**9 + 2000
                _report({"bridge_token": acc["bridge_token"], "trade_id": tid,
                         "mt5_ticket": base + 3, "status": "open",
                         "entry_price": 1.1, "order_ticket": base + 1,
                         "deal_ticket": base + 2, "position_id": base + 3,
                         "filled_volume": 0.60, "partial_fill": True,
                         "position_volume": 0.60})
                t = await db.trades.find_one({"_id": ins.inserted_id})
                assert t["partial_fill"] is True
                assert t["lot_size"] == 0.60 and t["original_lot_size"] == 0.60 and t["requested_lot_size"] == 1.00   # fix plan B8: baseline = FILLED
                ev = await db.trade_events.find_one(
                    {"event_type": "PartialFillAdopted", "trade_id": tid})
                assert ev and ev["filled_lots"] == 0.60
            finally:
                await db.trades.delete_one({"_id": ins.inserted_id})
                await db.trade_events.delete_many({"trade_id": tid})
        asyncio.run(run())

    def test_restart_during_execution_unresolved_then_resolved(self):
        """EA restarts mid-execution: unresolved ack (idempotent x2) →
        resolution replay opens → late unresolved replay ignored."""
        async def run():
            db = _db()
            acc = await _fixture(db)
            ins = await _mk_trade(db, acc, 0.30)
            tid = str(ins.inserted_id)
            try:
                base = int(time.time() * 1000) % 10**9 + 3000
                unres = {"bridge_token": acc["bridge_token"], "trade_id": tid,
                         "mt5_ticket": 0, "status": "pending",
                         "entry_price": 1.1, "error": "accepted_unresolved",
                         "order_ticket": base + 1}
                assert _report(unres).get("unresolved") is True
                assert _report(unres).get("unresolved") is True  # idempotent
                t = await db.trades.find_one({"_id": ins.inserted_id})
                assert t["status"] == "pending"
                assert t["submission_state"] == "broker_accepted_unresolved"
                assert not t.get("mt5_ticket")

                _report({"bridge_token": acc["bridge_token"], "trade_id": tid,
                         "mt5_ticket": base + 3, "status": "open",
                         "entry_price": 1.1, "replay": True,
                         "order_ticket": base + 1, "deal_ticket": base + 2,
                         "position_id": base + 3, "filled_volume": 0.30,
                         "position_volume": 0.30})
                t = await db.trades.find_one({"_id": ins.inserted_id})
                assert t["status"] == "open" and t["lot_size"] == 0.30

                late = _report(unres)
                assert late.get("ignored") == "already_open"
                t = await db.trades.find_one({"_id": ins.inserted_id})
                assert t["status"] == "open"
            finally:
                await db.trades.delete_one({"_id": ins.inserted_id})
                await db.trade_events.delete_many({"trade_id": tid})
        asyncio.run(run())


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
