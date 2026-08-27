"""iter-93 — Phase 6 full execution trace: 7-question composer, lifecycle
events, trace API."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient

from execution_trace import (compose, why_closed, why_opened, why_this_size,
                             why_this_stop, why_this_target, why_this_time)


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter93-{uuid.uuid4().hex[:8]}"

SIGNAL = {
    "confidence": 78, "min_confidence_required": 65,
    "engine_label": "MTF Confluence", "session": "london",
    "market_regime": {"key": "trending_up|normal",
                      "label": "TRENDING UP · NORMAL VOL"},
    "execution_timing": {"waited_ms": 800, "spread_before": 9.0,
                         "spread_after": 3.2},
    "monte_carlo": {"ev_r_net": 0.42, "p_tp_first": 0.61, "rr": 2.0},
    "effective_risk_pct": 1.0, "risk_amount": 100.0,
    "rr_ratio": 2.0, "kelly_f": 0.12,
    "engine_geometry": {"sl_atr_mult": 1.5},
    "key_factors": ["trend aligned", "liquidity clear"],
    "reasoning": "H1 uptrend with M15 pullback entry",
    "news_bias": "supportive",
}

TRADE = {
    "user_id": UID, "symbol": "XAUUSD", "action": "BUY", "status": "closed",
    "entry_price": 4000.5, "requested_price": 4000.0, "slippage_pips": 0.5,
    "stop_loss": 3990.0, "sl_pips": 30.0, "lot_size": 0.10,
    "tp1": 4010.0, "tp2": 4020.0, "tp3": 4030.0, "tp_pips": [20, 40, 60],
    "exit_price": 4020.0, "pnl": 195.0, "close_reason": "tp2_banked",
    "opened_at": "2026-07-24T08:00:00+00:00",
    "closed_at": "2026-07-24T11:30:00+00:00",
    "_dispatched_at": "2026-07-24T07:59:58+00:00",
    "acknowledged_at": "2026-07-24T08:00:01+00:00",
    "breakeven_set": True, "tp1_closed": True,
    "exit_vol_retarget": {"scale": 1.4, "atr_ratio": 1.45,
                          "at": "2026-07-24T09:00:00+00:00"},
}


# ------------------------------------------------------------ pure sections
def test_seven_answers_are_evidence_based():
    o = why_opened(TRADE, SIGNAL, {"stage": "risk_engine"})
    assert "MTF Confluence" in o["answer"] and "78%" in o["answer"]
    assert "+0.42R" in o["answer"]
    t = why_this_time(TRADE, SIGNAL)
    assert "london" in t["answer"] and "TRENDING UP" in t["answer"]
    assert "800ms" in t["answer"]
    s = why_this_size(TRADE, SIGNAL)
    assert "1.0% of equity" in s["answer"] and "0.1 lots" in s["answer"]
    st = why_this_stop(TRADE, SIGNAL)
    assert "30.0 pips" in st["answer"] and "1.5× ATR" in st["answer"]
    assert "0.5 pip" in st["answer"]
    tg = why_this_target(TRADE, SIGNAL)
    assert "4010" in tg["answer"] and "2.0:1" in tg["answer"]
    assert "61%" in tg["answer"]


def test_why_closed_and_open_state(db):
    async def run():
        c = await why_closed(db, {**TRADE, "_id": "x"})
        assert "tp2_banked" in c["answer"] and "$195.0" in c["answer"]
        assert c["evidence"]["duration"] == "3h30m"
        o = await why_closed(db, {**TRADE, "_id": "x", "status": "open"})
        assert "not closed yet" in o["answer"]
    _run(run())


# ------------------------------------------------------------ full compose
def test_compose_end_to_end_with_events(db):
    async def run():
        from trade_events import append, build
        sid = (await db.signals.insert_one(dict(SIGNAL, user_id=UID))).inserted_id
        tid = (await db.trades.insert_one(
            dict(TRADE, signal_id=str(sid)))).inserted_id
        await append(db, build("ModificationConfirmed", user_id=UID,
                               trade_id=str(tid), symbol="XAUUSD",
                               source="bridge_ack",
                               payload={"detail": "MODIFY_SL confirmed by broker (SL 4000.5)"}))
        trade = await db.trades.find_one({"_id": tid})
        trace = await compose(db, trade)
        assert set(trace) >= {"why_opened", "why_this_time", "why_this_size",
                              "why_this_stop", "why_this_target",
                              "what_changed", "why_closed"}
        tl = trace["what_changed"]["timeline"]
        kinds = [e["kind"] for e in tl]
        assert "DISPATCHED" in kinds and "FILLED" in kinds and "CLOSED" in kinds
        assert "ModificationConfirmed" in kinds
        assert "EXIT_RETARGET_VOL" in kinds and "BREAKEVEN" in kinds
        # dated events chronological
        dated = [e["at"] for e in tl if e["at"]]
        assert dated == sorted(dated)
        assert "adjustment" in trace["what_changed"]["answer"]
        # cleanup
        await db.trades.delete_one({"_id": tid})
        await db.signals.delete_one({"_id": sid})
        await db.trade_events.delete_many({"user_id": UID})
    _run(run())


def test_compose_handles_missing_signal(db):
    async def run():
        tid = (await db.trades.insert_one(
            {**{k: v for k, v in TRADE.items()}, "signal_id": None})).inserted_id
        trade = await db.trades.find_one({"_id": tid})
        trace = await compose(db, trade)
        assert trace["why_opened"]["answer"]     # graceful, no crash
        assert trace["why_this_size"]["answer"]
        await db.trades.delete_one({"_id": tid})
    _run(run())


def test_new_event_types_registered():
    from trade_events import EVENT_TYPES, build
    for et in ("TargetsRescaled", "StopTightened", "PartialCloseRequested",
               "ModificationConfirmed"):
        assert et in EVENT_TYPES
        ev = build(et, user_id=UID, trade_id="t1", payload={"detail": "x"})
        assert ev["event_type"] == et and ev["payload"]["detail"] == "x"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
