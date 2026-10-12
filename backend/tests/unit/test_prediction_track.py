"""Prediction track record — resolve decisions against later candles, persist finals, calibration."""
import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.unit

import prediction_track as pt  # noqa: E402
from fake_mongo import FakeDb  # noqa: E402

T0 = 1_760_000_000 - 1_760_000_000 % 900          # a bar boundary


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def _bars(path, t0=T0):
    """path: list of (high, low, close) per M15 bar."""
    return [{"t": t0 + i * 900, "o": c, "h": h, "l": lo, "c": c} for i, (h, lo, c) in enumerate(path)]


def _dec(action="BUY", entry=100.0, sl=99.0, tp=102.0, t=T0, **kw):
    sig = {"action": action, "entry_price": entry, "stop_loss": sl, "take_profit": tp,
           "confidence": kw.pop("confidence", 72), "mc": {"p_tp_first": kw.pop("p", 0.4)}}
    return {"ts": _iso(t), "symbol": kw.pop("symbol", "XAUUSD.r"), "status": kw.pop("status", "executed"),
            "stage": "execution", "signal": sig, **kw}


def test_resolve_tp_first():
    bars = _bars([(100.5, 99.5, 100.2), (102.1, 100.1, 102.0)])
    assert pt.resolve(_dec(), bars) == {"outcome": "tp", "bars": 2}


def test_resolve_sl_first_and_same_bar_is_sl():
    assert pt.resolve(_dec(), _bars([(100.4, 98.9, 99.2)]))["outcome"] == "sl"
    assert pt.resolve(_dec(), _bars([(102.5, 98.5, 100.0)]))["outcome"] == "sl"


def test_resolve_sell_mirror():
    d = _dec("SELL", entry=100, sl=101, tp=98)
    assert pt.resolve(d, _bars([(100.2, 97.9, 98.1)]))["outcome"] == "tp"


def test_resolve_ignores_bars_before_the_decision():
    bars = _bars([(103, 99.5, 100)] + [(100.3, 99.7, 100)] * 3)      # TP touched BEFORE the decision
    r = pt.resolve(_dec(t=T0 + 900), bars, horizon=3)
    assert r["outcome"] == "expired" and r["drift_r"] == pytest.approx(0.0)


def test_resolve_pending_no_data_and_bad_levels():
    assert pt.resolve(_dec(), _bars([(100.3, 99.7, 100)] * 5), horizon=10)["outcome"] == "pending"
    assert pt.resolve(_dec(), [])["outcome"] == "pending"
    assert pt.resolve(_dec(t=T0 - 86400), _bars([(100.3, 99.7, 100)]))["outcome"] == "no_data"
    assert pt.resolve(_dec(sl=101.0), _bars([(100.3, 99.7, 100)]))["outcome"] == "no_data"   # SL above a BUY entry
    assert pt.resolve({"ts": "garbage", "signal": {}}, _bars([(1, 1, 1)]))["outcome"] == "no_data"


def test_wilson_interval():
    lo, hi = pt.wilson(50, 100)
    assert lo < 0.5 < hi and hi - lo < 0.21
    assert pt.wilson(0, 0) == (None, None)


def test_calibration_buckets_brier_and_status_split():
    rows = ([{"outcome": "tp", "confidence": 85, "p_tp_first": 0.8, "status": "executed"}] * 8
            + [{"outcome": "sl", "confidence": 85, "p_tp_first": 0.8, "status": "executed"}] * 2
            + [{"outcome": "tp", "confidence": 55, "p_tp_first": 0.3, "status": "rejected"}] * 3
            + [{"outcome": "sl", "confidence": 55, "p_tp_first": 0.3, "status": "rejected"}] * 7
            + [{"outcome": "expired", "confidence": 90, "status": "executed"}])
    cal = pt.calibration(rows)
    assert cal["decided"] == 20 and cal["hit_rate"] == 0.55
    b80 = next(b for b in cal["by_confidence"] if b["bucket"] == "80-90")
    assert (b80["n"], b80["hit_rate"]) == (10, 0.8)
    p08 = next(b for b in cal["by_probability"] if b["bucket"] == "0.8-0.9")
    assert p08["mean_predicted"] == 0.8 and p08["hit_rate"] == 0.8
    assert cal["brier"] == pytest.approx((8 * 0.04 + 2 * 0.64 + 3 * 0.49 + 7 * 0.09) / 20, abs=1e-4)
    assert cal["by_status"]["rejected"]["hit_rate"] == 0.3


def test_calibration_top_edge_inclusive():
    cal = pt.calibration([{"outcome": "tp", "confidence": 100, "p_tp_first": 1.0}])
    assert cal["by_confidence"][-1]["n"] == 1 and cal["by_probability"][-1]["n"] == 1


def _seed(db, decisions, path, t_basis="utc"):
    db.trade_decisions.rows.extend({**d, "user_id": "u1"} for d in decisions)
    db.intraday_candles.rows.append({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M15",
                                     "t_basis": t_basis, "bars": _bars(path)})


def test_track_record_persists_only_final_outcomes_and_scopes_by_symbol():
    db = FakeDb()
    path = [(100.3, 99.7, 100)] * 4 + [(102.5, 100.0, 102.2)] + [(102.3, 101.9, 102)] * 3
    _seed(db, [_dec(), _dec(t=T0 + 7 * 900, entry=102, sl=101, tp=104),   # resolves tp · pending
               _dec(symbol="EURUSD", entry=1.1, sl=1.09, tp=1.12)], path)   # other symbol
    out = asyncio.run(pt.track_record(db, "u1", "XAUUSD"))
    assert [r["outcome"] for r in out["recent"]] == ["pending", "tp"]
    assert out["pending"] == 1
    assert [r["outcome"] for r in db.prediction_outcomes.rows] == ["tp"]
    assert out["calibration"]["decided"] == 1 and out["calibration"]["hit_rate"] == 1.0

    # a stored outcome is reused, never re-resolved (candles may have rolled past)
    db.intraday_candles.rows[0]["bars"] = []
    again = asyncio.run(pt.track_record(db, "u1", "XAUUSD"))
    assert [r["outcome"] for r in again["recent"]] == ["pending", "tp"]
    assert len(db.prediction_outcomes.rows) == 1


def test_non_utc_candle_clock_never_stores_outcomes():
    db = FakeDb()
    _seed(db, [_dec()], [(102.5, 100.0, 102.2)], t_basis="broker_unknown")
    out = asyncio.run(pt.track_record(db, "u1", "XAUUSD"))
    assert out["recent"][0]["outcome"] == "pending" and not db.prediction_outcomes.rows


def test_other_users_decisions_are_invisible():
    db = FakeDb()
    _seed(db, [_dec()], [(102.5, 100.0, 102.2)])
    db.trade_decisions.rows[0]["user_id"] = "someone_else"
    out = asyncio.run(pt.track_record(db, "u1", "XAUUSD"))
    assert out["recent"] == [] and not db.prediction_outcomes.rows
