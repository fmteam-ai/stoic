"""Fix plan step C1 — candle data (A1, R3, A3, A10, A11, R11).
A1   candles read per user + timeframe (no cross-user fallback); bars validated at ingest.
R3   EA bar times are broker time → normalised to UTC with the broker offset before the forming-bar check.
A3   no session VWAP before the first closed bar of the UTC day → VWAP engine idle, never "price at VWAP".
A10  broker tick (forming-bar close) is the live price when fresh; public quote is the fallback.
A11  forecast cache keyed per user.
R11  H8 bar/open-time anchor (UTC, skip when unknown), H9 entry-time stop in training, signals read scoped to user.
Pure unit tests (fake async db) — run with DB_NAME="".
"""
import asyncio
import inspect
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "unit"))
from fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _bar(t, o=100.0, h=101.0, lo=99.0, c=100.5, v=10):
    return {"t": t, "o": o, "h": h, "l": lo, "c": c, "v": v}


def _now_slot(tf=900):
    now = int(datetime.now(timezone.utc).timestamp())
    return now - now % tf


def test_a1_validate_bars_drops_bad_geometry_and_misaligned_timestamps():
    from routes.bridge_routes import validate_bars
    t0 = _now_slot() - 900 * 10
    raw = [_bar(t0), _bar(t0 + 900, h=99.5),                 # high below open/close
           _bar(t0 + 1800, lo=100.9),                        # low above open/close
           _bar(t0 + 2700, c=float("nan")), _bar(t0 + 3600, o=-1),
           _bar(t0 + 4500 + 7),                              # not aligned to M15
           _bar(t0 - 40 * 86400),                            # 40 days old
           _bar(t0 + 5400), _bar(t0 + 5400, c=100.7),        # duplicate ts → last wins
           {"t": "x"}, "garbage"]
    bars, dropped = validate_bars(raw, "M15")
    assert [b["t"] for b in bars] == [t0, t0 + 5400] and dropped == 8
    assert bars[1]["c"] == 100.7 and all(set(b) == {"t", "o", "h", "l", "c", "v"} for b in bars)


def test_r3_broker_offset_sources():
    from routes.bridge_routes import broker_offset_sec
    assert broker_offset_sec({}) == (0, "unknown")
    assert broker_offset_sec({"broker_utc_offset_sec": 7200}) == (7200, "learned_from_deals")
    assert broker_offset_sec({"broker_utc_offset_sec": 7200, "broker_time_info": {"server_gmt_offset_sec": 10800}}) == (10800, "ea_broker_time")
    assert broker_offset_sec({"broker_time_info": {"server_gmt_offset_sec": 99999}}) == (0, "unknown")   # implausible → ignored


def test_r3_receive_candles_normalises_broker_time_and_stores_tick():
    import routes.bridge_routes as br
    db = FakeDb()
    acc = {"_id": "acc1", "user_id": "u1", "broker_time_info": {"server_gmt_offset_sec": 10800}}
    slot = _now_slot()                                             # UTC open of the forming M15 bar
    # EA sends broker-time epochs (UTC+3): closed bar, and the forming bar
    payload = br.BridgeCandles(bridge_token="t", symbol="XAUUSD.fx", timeframe="M15",
                               bars=[_bar(slot - 900 + 10800, c=100.2), _bar(slot + 10800, h=105.0, c=104.5)])

    async def fake_acc(token):
        return acc
    with patch.object(br, "get_db", return_value=db), patch.object(br, "_account_by_token", fake_acc):
        out = run(br.receive_candles(payload))
    assert out["stored"] == 1
    doc = db.intraday_candles.rows[0]
    assert doc["user_id"] == "u1" and doc["symbol"] == "XAUUSD" and doc["timeframe"] == "M15"
    assert [b["t"] for b in doc["bars"]] == [slot - 900]           # UTC, forming bar excluded
    assert doc["t_basis"] == "utc" and doc["broker_offset_sec"] == 10800 and doc["offset_source"] == "ea_broker_time"
    assert doc["last_tick"]["price"] == 104.5 and doc["last_tick"]["bar_t"] == slot          # A10 broker tick
    health = db.candle_feed_health.rows[0]
    assert health["forming_dropped"] == 1 and health["valid_bars"] == 1 and health["broker_offset_sec"] == 10800
    # legacy broker-time doc is migrated once on merge
    db.intraday_candles.rows[0].pop("t_basis")
    db.intraday_candles.rows[0]["bars"] = [_bar(slot - 1800 + 10800)]
    with patch.object(br, "get_db", return_value=db), patch.object(br, "_account_by_token", fake_acc):
        run(br.receive_candles(payload))
    assert [b["t"] for b in db.intraday_candles.rows[0]["bars"]] == [slot - 1800, slot - 900]
    # without any offset knowledge: still UTC-tagged with offset 0 / unknown (visible in health)
    db2 = FakeDb()

    async def plain_acc(token):
        return {"_id": "acc2", "user_id": "u2"}
    with patch.object(br, "get_db", return_value=db2), patch.object(br, "_account_by_token", plain_acc):
        run(br.receive_candles(br.BridgeCandles(bridge_token="t", symbol="XAUUSD", bars=[_bar(slot - 900), _bar(slot)])))
    assert db2.intraday_candles.rows[0]["offset_source"] == "unknown" and db2.candle_feed_health.rows[0]["offset_source"] == "unknown"


def test_a1_reads_are_scoped_to_user_and_timeframe():
    import intraday_features as f
    db = FakeDb()
    now = datetime.now(timezone.utc).isoformat()
    bars = [_bar(_now_slot() - 900 * (40 - i), c=100 + i * 0.1) for i in range(40)]
    db.intraday_candles.rows += [{"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M15", "bars": bars, "updated_at": now,
                                  "last_tick": {"price": 4010.0, "at": now}},
                                 {"user_id": "u1", "symbol": "XAUUSD", "timeframe": "H1", "bars": bars[:5], "updated_at": now},
                                 {"user_id": "u2", "symbol": "XAUUSD", "timeframe": "M15", "bars": bars, "updated_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}]
    with patch("database.get_db", return_value=db):
        assert run(f.load_user_candles("XAUUSD.fx", None)) is None                       # no user → nothing (never shared)
        assert len(run(f.load_user_candles("XAUUSD.fx", "u1"))["bars"]) == 40
        assert len(run(f.load_user_candles("XAUUSD", "u1", "H1"))["bars"]) == 5
        assert run(f.load_user_candles("XAUUSD", "u2")) is None                           # stale stream
        assert run(f.load_user_candles("XAUUSD", "u3")) is None                           # other user's bars never leak
        assert run(f.broker_live_price("XAUUSD", "u1")) == 4010.0                         # A10 fresh tick
        db.intraday_candles.rows[0]["last_tick"]["at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        assert run(f.broker_live_price("XAUUSD", "u1")) is None                           # stale tick → public quote
        assert run(f.broker_live_price("XAUUSD", None)) is None
    import ai_signals, mtf_intraday
    src = inspect.getsource(ai_signals.analyze_symbol)
    assert "broker_live_price(symbol, user_id)" in src and 'price_source = "broker_tick"' in src and "fetch_intraday_pack(symbol, user_id)" in src
    assert "user_id=user_id" in src                                                       # mtf confluence scoped
    assert "load_user_candles" in inspect.getsource(mtf_intraday.fetch_mtf_confluence)
    assert "user_id=cfg.get(\"user_id\")" in open(os.path.join(os.path.dirname(ai_signals.__file__), "agents", "strategy_agent.py")).read()
    for mod in ("regime_intelligence", "champion_challenger2", "ablation", "shadow_benchmark", "digital_twin"):
        s = open(os.path.join(os.path.dirname(ai_signals.__file__), f"{mod}.py")).read()
        assert 'or await db.intraday_candles.find_one(\n        {"symbol"' not in s and 'find_one({"symbol": sym' not in s


def test_a3_no_vwap_before_first_bar_of_day():
    from intraday_features import compute_intraday_features
    from strategy_engines import hf_scalp_signal
    # 60 bars that all closed YESTERDAY (UTC) → no session VWAP yet
    today0 = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    bars = [_bar(today0 - 900 * (61 - i), o=100 + i * 0.05, h=100.3 + i * 0.05, lo=99.8 + i * 0.05, c=100.1 + i * 0.05) for i in range(60)]
    feats = compute_intraday_features(bars)
    assert feats is not None and feats["session_vwap"] is None and feats["vwap_dist_pct"] is None
    action, reason = hf_scalp_signal(feats)
    assert action is None and "no session VWAP" in reason
    action, reason = hf_scalp_signal(feats, fast=True)
    assert action is None and "no session VWAP" in reason
    # once today's bars exist the VWAP is defined again
    bars2 = bars + [_bar(today0 + 900 * i, o=103 + i * 0.05, h=103.3 + i * 0.05, lo=102.8 + i * 0.05, c=103.1 + i * 0.05) for i in range(4)]
    feats2 = compute_intraday_features(bars2)
    assert feats2["session_vwap"] is not None and feats2["vwap_dist_pct"] is not None


def test_a11_forecast_cache_per_user():
    import forecast_agent as fa
    src = inspect.getsource(fa.get_forecast)
    assert 'ckey = f"{user_id}:{base}"' in src and "_cache[ckey]" in src and '"timeframe": "M15"' in src
    fa._cache.clear()
    fa._cache["u1:XAUUSD"] = (fa.time.time() + 60, {"source": "M15", "who": "u1"})
    with patch.dict(os.environ, {"FORECAST_AGENT_ENABLED": "true"}):
        assert run(fa.get_forecast(FakeDb(), "u1", "XAUUSD.fx"))["who"] == "u1"
        assert "u2:XAUUSD" not in fa._cache                                               # u2 does not see u1's forecast
    fa._cache.clear()


def test_r11_training_rows_entry_time_stop_and_open_time_anchor():
    import ml_ensemble as me
    from bson import ObjectId
    sid1, sid2 = ObjectId(), ObjectId()
    db = FakeDb()
    db.signals.rows += [{"_id": sid1, "user_id": "u1", "stop_loss": 3990.0, "entry_price": 4000.0, "confidence": 70},
                        {"_id": sid2, "user_id": "u2", "stop_loss": 1.0, "entry_price": 2.0}]           # another user's signal
    trades = [
        {"user_id": "u1", "signal_id": str(sid1), "symbol": "XAUUSD", "action": "BUY", "pnl": 12.0, "entry_price": 4000.0,
         "stop_loss": 4003.0, "opened_at": "2026-10-05T07:15:00Z"},                                       # trailed stop at close
        {"user_id": "u1", "signal_id": str(sid2), "symbol": "XAUUSD", "action": "SELL", "pnl": -5.0, "entry_price": 4000.0,
         "stop_loss": 4000.5, "sl_pips": 100, "opened_at": "2026-10-05T09:00:00+02:00"},                   # cross-user signal ignored → sl_pips
        {"user_id": "u1", "signal_id": "", "symbol": "XAUUSD", "action": "BUY", "pnl": 3.0, "entry_price": 4000.0, "stop_loss": 3995.0},  # no open time → skipped
        {"user_id": "u1", "signal_id": "", "symbol": "XAUUSD", "action": "BUY", "pnl": 0, "opened_at": "2026-10-05T07:15:00Z"},          # flat → skipped
    ]
    assert me.entry_time_stop(trades[0], db.signals.rows[0]) == 3990.0                                    # H9 signal stop, not 4003
    assert me.entry_time_stop(trades[1], {}) == 4010.0                                                    # 100 pips above a SELL entry
    assert me.entry_time_stop({"stop_loss": 1.5}, {}) == 1.5 and me.entry_time_stop({}, {}) is None
    assert me.trade_open_time(trades[0]).hour == 7 and me.trade_open_time(trades[1]).hour == 7            # H8 UTC anchor
    assert me.trade_open_time(trades[2]) is None
    X, y = run(me.build_training_rows(db, "u1", trades))
    assert len(X) == 2 and y == [1, 0]
    sl_idx = me.FEATURES.index("sl_pips") if hasattr(me, "FEATURES") else -2
    assert X[0][sl_idx] == pytest.approx(100.0, rel=0.05) and X[1][sl_idx] == pytest.approx(100.0, rel=0.05)
    import learning_pipeline as lp
    assert "build_training_rows" in inspect.getsource(lp._build_xy)
