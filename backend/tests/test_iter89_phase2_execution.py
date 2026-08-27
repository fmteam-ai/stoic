"""iter-89 — Phase 2 execution intelligence: broker score, execution
timing, adaptive exit engine."""
import asyncio
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter89-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------- broker intel
def test_spread_component():
    from broker_intel import spread_component
    good = spread_component({"XAUUSD": 2.0})   # typical gold ≈3.5p
    assert good["score"] == 100.0
    bad = spread_component({"XAUUSD": 14.0})   # 4× typical
    assert bad["score"] < 40
    assert spread_component(None)["score"] is None


def test_slippage_and_fill_components():
    from broker_intel import fill_speed_component, slippage_component
    now = datetime.now(timezone.utc)
    trades = [{"symbol": "XAUUSD", "requested_price": 4000.0,
               "entry_price": 4000.05,
               "_dispatched_at": now.isoformat(),
               "acknowledged_at": (now + timedelta(seconds=2)).isoformat()}]
    s = slippage_component(trades)
    assert s["n"] == 1 and 0 < s["avg_slippage_pips"] < 1
    assert s["score"] > 80
    f = fill_speed_component(trades)
    assert f["avg_fill_sec"] == 2.0 and f["score"] > 90
    assert slippage_component([])["score"] is None


def test_reject_component():
    from broker_intel import reject_component
    ok = reject_component([{}] * 20, [])
    assert ok["score"] == 100.0
    bad = reject_component([{}] * 8, [{"error": "retcode=10004 requote"},
                                      {"error": "retcode=10016"}])
    assert bad["requotes"] == 1
    assert bad["score"] < 100


def test_score_account_end_to_end(db):
    from broker_intel import score_account

    async def run():
        acc_id = None
        try:
            now = datetime.now(timezone.utc)
            res = await db.accounts.insert_one({
                "user_id": UID, "label": "TB", "status": "active",
                "bridge_token": f"iter89-{uuid.uuid4().hex}",
                "current_spreads": {"XAUUSD": 3.0},
                "symbol_specs": {"XAUUSD": {"stops_level": 0, "freeze_level": 0}},
                "last_heartbeat": now.isoformat()})
            acc_id = res.inserted_id
            for i in range(5):
                await db.trades.insert_one({
                    "user_id": UID, "account_id": str(acc_id), "origin": "auto",
                    "status": "closed", "symbol": "XAUUSD",
                    "requested_price": 4000.0, "entry_price": 4000.03,
                    "_dispatched_at": now.isoformat(),
                    "acknowledged_at": (now + timedelta(seconds=1.5)).isoformat(),
                    "opened_at": now.isoformat()})
            acc = await db.accounts.find_one({"_id": acc_id})
            out = await score_account(db, acc)
            assert out["score"] is not None and out["score"] > 70
            assert out["provisional"] is False
            assert out["fills_measured"] == 5
            assert set(out["components"]) == {"spread", "slippage",
                                              "fill_speed", "rejects", "freeze"}
        finally:
            await db.trades.delete_many({"user_id": UID})
            if acc_id:
                await db.accounts.delete_one({"_id": acc_id})
    _run(run())


def test_deterioration_detection(db):
    from broker_intel import detect_deterioration

    async def run():
        acc = f"det-{uuid.uuid4().hex[:8]}"
        try:
            base_t = datetime.now(timezone.utc) - timedelta(days=3)
            for i in range(6):
                await db.broker_intel_scores.insert_one(
                    {"account_id": acc, "score": 90.0,
                     "at": base_t + timedelta(hours=i)})
            det = await detect_deterioration(db, acc, 70.0)
            assert det and det["drop"] == 20.0
            assert await detect_deterioration(db, acc, 88.0) is None
        finally:
            await db.broker_intel_scores.delete_many({"account_id": acc})
    _run(run())


# ------------------------------------------------------- execution timing
def test_timing_decision():
    from execution_timing import _HIST, decide, record_spread
    acc = f"t-{uuid.uuid4().hex[:6]}"
    # not enough history → send now
    assert decide(acc, "XAUUSD")["delay_ms"] == 0
    for _ in range(12):
        record_spread(acc, {"XAUUSD.fx": 3.0})
    assert decide(acc, "XAUUSD")["delay_ms"] == 0        # normal spread
    record_spread(acc, {"XAUUSD.fx": 9.0})               # 3× spike
    v = decide(acc, "XAUUSD")
    assert 0 < v["delay_ms"] <= 2000
    assert "median" in v["reason"]
    _HIST.clear()


def test_consider_delay_records_outcome(db):
    from execution_timing import _HIST, consider_delay, record_spread

    async def run():
        acc_id = f"cd-{uuid.uuid4().hex[:8]}"
        try:
            for _ in range(12):
                record_spread(acc_id, {"XAUUSD": 3.0})
            record_spread(acc_id, {"XAUUSD": 9.0})
            v = await consider_delay(db, {"_id": acc_id}, "XAUUSD")
            assert v["waited_ms"] > 0
            doc = await db.execution_timing_stats.find_one({"account_id": acc_id})
            assert doc and doc["spread_before"] == 9.0
        finally:
            await db.execution_timing_stats.delete_many({"account_id": acc_id})
            _HIST.clear()
    _run(run())


# ------------------------------------------------------- adaptive exits
def _bars(n=60, rng=2.0, close_seq=None, base=4000.0):
    now = time.time()
    out = []
    px = base
    for i in range(n):
        c = close_seq[i - (n - len(close_seq))] if close_seq and i >= n - len(close_seq) else px
        out.append({"t": now - (n - 1 - i) * 900, "o": c, "h": c + rng,
                    "l": c - rng, "c": c, "v": 100})
    return out


def test_compute_features():
    from adaptive_exits import compute_features
    feats = compute_features(_bars())
    assert feats and feats["atr15"] > 0
    assert compute_features(_bars(n=10)) is None


def test_vol_retarget_expansion():
    from adaptive_exits import compute_features, vol_retarget
    feats = compute_features(_bars(rng=4.0))       # ATR ≈ 8
    trade = {"symbol": "XAUUSD", "sl_pips": 30.0,  # implied entry ATR = 2.0
             "tp_pips": [20.0, 40.0, 60.0], "entry_price": 4000.0}
    act = vol_retarget(trade, feats, pips_up=5.0)
    assert act and act["kind"] == "EXIT_RETARGET_VOL"
    assert act["scale"] == 1.6                     # capped
    assert act["new_tp_pips"][0] == 32.0
    # already retargeted → no-op
    trade["exit_vol_retarget"] = {"scale": 1.6}
    assert vol_retarget(trade, feats, 5.0) is None


def test_fade_tighten_only_tightens():
    from adaptive_exits import compute_features, fade_tighten
    closes = [4000 + i * 0.5 for i in range(20)] + [4009.5, 4009.0, 4008.5]
    feats = compute_features(_bars(rng=1.0, close_seq=closes))
    trade = {"symbol": "XAUUSD", "action": "BUY", "entry_price": 4000.0,
             "stop_loss": 3997.0, "sl_pips": 30.0}
    act = fade_tighten(trade, feats, current=4008.5, pips_up=85.0)
    assert act and act["kind"] == "EXIT_TIGHTEN_FADE"
    assert 4000.0 < act["new_sl"] < 4008.5         # locks profit, below price
    # existing SL already tighter → never loosen
    trade["stop_loss"] = 4007.0
    assert fade_tighten(trade, feats, 4008.5, 85.0) is None
    # not enough profit → no-op
    trade["stop_loss"] = 3997.0
    assert fade_tighten(trade, feats, 4001.0, 10.0) is None


def test_resistance_derisk():
    from adaptive_exits import compute_features, resistance_derisk
    feats = compute_features(_bars(rng=2.0))
    trade = {"symbol": "XAUUSD", "action": "BUY", "entry_price": 3995.0,
             "lot_size": 0.20}
    near = feats["donchian_high"] - 0.1 * feats["atr15"]
    act = resistance_derisk(trade, feats, current=near, pips_up=50.0)
    assert act and act["kind"] == "EXIT_DERISK_RESISTANCE"
    assert act["new_volume"] == 0.15
    far = feats["donchian_high"] - 3.0 * feats["atr15"]
    assert resistance_derisk(trade, feats, far, 50.0) is None
    trade["exit_derisked"] = {"at": "x"}
    assert resistance_derisk(trade, feats, near, 50.0) is None


def test_manage_exits_wiring(db):
    """End-to-end: open trade + shock ATR candles → TP ladder retargeted."""
    from adaptive_exits import manage_exits

    async def run():
        tid = None
        try:
            now = time.time()
            bars = [{"t": now - (59 - i) * 900, "o": 4000.0, "h": 4004.0,
                     "l": 3996.0, "c": 4000.0, "v": 100} for i in range(60)]
            await db.intraday_candles.insert_one(
                {"user_id": UID, "symbol": "XAUUSD", "timeframe": "M15",
                 "bars": bars, "updated_at": datetime.now(timezone.utc)})
            res = await db.trades.insert_one({
                "user_id": UID, "symbol": "XAUUSD", "action": "BUY",
                "status": "open", "entry_price": 4000.0, "stop_loss": 3997.0,
                "sl_pips": 30.0, "tp_pips": [20.0, 40.0, 60.0],
                "lot_size": 0.10, "origin": "auto"})
            tid = res.inserted_id
            trade = await db.trades.find_one({"_id": tid})
            acted = await manage_exits(db, trade, {"adaptive_exits_enabled": True},
                                       current=4000.5, pips_up=5.0)
            assert acted is True
            t = await db.trades.find_one({"_id": tid})
            assert t.get("exit_vol_retarget")
            assert t["tp_pips"][0] > 20.0
            # disabled toggle short-circuits
            acted2 = await manage_exits(db, t, {"adaptive_exits_enabled": False},
                                        4000.5, 5.0)
            assert acted2 is False
        finally:
            await db.intraday_candles.delete_many({"user_id": UID})
            if tid:
                await db.trades.delete_one({"_id": tid})
    _run(run())


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
