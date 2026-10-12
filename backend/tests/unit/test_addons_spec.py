"""Add-ons Build Spec (Oct 12 2026) — A.8 prediction track (10), B.12 chart patterns (11), C1–C3 hardening."""
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
T0 = 1_760_000_000  # aligned unix seconds


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8").read()


def _bars(closes, t0=T0, spread=1.0):
    out = []
    for k, c in enumerate(closes):
        o = closes[k - 1] if k else c
        out.append({"t": t0 + k * 900, "o": o, "h": max(o, c) + spread, "l": min(o, c) - spread, "c": c, "v": 1})
    return out


def _dec(action="BUY", entry=100.0, sl=90.0, tp=120.0, ts=T0, **kw):
    d = {"_id": ObjectId(), "user_id": "u1", "symbol": "XAUUSD#", "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
         "status": "executed", "stage": "x", "signal": {"action": action, "entry_price": entry, "stop_loss": sl, "take_profit": tp, "confidence": 72, "mc": {"p_tp_first": 0.6}}}
    d.update(kw)
    return d


# ── A ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_a1_buy_tp_first_and_sell_mirror():
    import prediction_track as pt
    bars = _bars([100, 105, 110, 121, 125], spread=0.5)
    assert pt.resolve(_dec(), bars) == {"outcome": "tp", "bars": 4}
    bars = _bars([100, 95, 90, 79], spread=0.5)
    assert pt.resolve(_dec("SELL", 100, 110, 80), bars) == {"outcome": "tp", "bars": 4}


def test_a2_sl_and_tp_same_bar_is_sl():
    import prediction_track as pt
    bars = _bars([100, 100], spread=0.5)
    bars[1].update({"h": 130, "l": 80})
    assert pt.resolve(_dec(), bars)["outcome"] == "sl"


def test_a3_wrong_side_or_missing_stop_is_no_data():
    import prediction_track as pt
    bars = _bars([100, 101])
    assert pt.resolve(_dec(tp=80), bars)["outcome"] == "no_data"            # BUY with tp below entry
    assert pt.resolve(_dec(sl=None), bars)["outcome"] == "no_data"
    assert pt.resolve(_dec("HOLD"), bars)["outcome"] == "no_data"
    assert pt.resolve(_dec(entry=-1), bars)["outcome"] == "no_data"


def test_a4_pending_not_stored_then_final_stored():
    import prediction_track as pt
    db = FakeDb()
    d = _dec()
    _run(db.trade_decisions.insert_one(d))
    _run(db.intraday_candles.insert_one({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M15", "t_basis": "utc", "bars": _bars([100, 101, 102], spread=0.2)}))
    out = _run(pt.sync_outcomes(db, "u1", "XAUUSD#"))
    assert out["stored_new"] == 0 and out["pending"][0]["outcome"] == "pending" and _run(db.prediction_outcomes.count_documents({})) == 0
    _run(db.intraday_candles.update_one({"user_id": "u1"}, {"$set": {"bars": _bars([100, 101, 102, 121], spread=0.2)}}))
    out = _run(pt.sync_outcomes(db, "u1", "XAUUSD#"))
    row = _run(db.prediction_outcomes.find_one({"decision_id": str(d["_id"])}))
    assert out["stored_new"] == 1 and row["outcome"] == "tp" and row["bars"] == 4 and row["resolved_at"] and row["base"] == "XAUUSD"
    assert _run(db.trade_decisions.find_one({"_id": d["_id"]})) == d                  # never modified


def test_a5_expired_with_drift_sign():
    import prediction_track as pt
    bars = _bars([100 + 0.05 * k for k in range(100)], spread=0.1)
    r = pt.resolve(_dec(entry=100, sl=90, tp=200), bars)
    assert r["outcome"] == "expired" and r["bars"] == 96 and r["drift_r"] > 0
    r = pt.resolve(_dec("SELL", entry=100, sl=110, tp=1), bars)
    assert r["outcome"] == "expired" and r["drift_r"] < 0


def test_a6_decision_before_first_bar_is_no_data():
    import prediction_track as pt
    assert pt.resolve(_dec(ts=T0 - 5000), _bars([100, 101]))["outcome"] == "no_data"
    assert pt.resolve(_dec(), [])["outcome"] == "pending"


def test_a7_non_utc_basis_all_pending():
    import prediction_track as pt
    db = FakeDb()
    _run(db.trade_decisions.insert_one(_dec()))
    _run(db.intraday_candles.insert_one({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M15", "t_basis": "broker", "bars": _bars([100, 121, 125])}))
    out = _run(pt.sync_outcomes(db, "u1", "XAUUSD"))
    assert out["stored_new"] == 0 and out["pending"][0]["reason"] == "candle clock not UTC-aligned yet"


def test_a8_stored_outcome_reused_not_recomputed():
    import prediction_track as pt
    db = FakeDb()
    d = _dec()
    _run(db.trade_decisions.insert_one(d))
    _run(db.intraday_candles.insert_one({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M15", "t_basis": "utc", "bars": _bars([100, 121], spread=0.2)}))
    _run(pt.sync_outcomes(db, "u1", "XAUUSD"))
    _run(db.intraday_candles.update_one({"user_id": "u1"}, {"$set": {"bars": _bars([100, 85], spread=0.2)}}))
    _run(pt.sync_outcomes(db, "u1", "XAUUSD"))
    assert _run(db.prediction_outcomes.find_one({"decision_id": str(d["_id"])}))["outcome"] == "tp"


def test_a9_wilson_and_calibration():
    import prediction_track as pt
    assert pt.wilson(0, 0) == [None, None]
    lo, hi = pt.wilson(10, 10)
    assert hi == 1.0 and 0.7 < lo < 0.75
    rows = [{"outcome": "tp", "confidence": 85, "p_tp_first": 0.8, "status": "executed"}, {"outcome": "sl", "confidence": 55, "p_tp_first": 0.3, "status": "rejected"},
            {"outcome": "expired", "confidence": 99, "p_tp_first": 0.9, "status": "executed"}, {"outcome": "tp", "confidence": 100, "p_tp_first": 1.0, "status": "rejected"}]
    c = pt.calibration(rows)
    assert c["decided"] == 3 and c["hits"] == 2 and c["by_status"]["rejected"] == {"n": 2, "tp": 1, "hit_rate": 0.5}
    assert any(b["bucket"] == "90-100" and b["n"] == 1 for b in c["by_confidence"]) and c["brier"] is not None
    assert any(b["bucket"] == "0.9-1.0" for b in c["by_probability"])


def test_a10_route_422_and_user_isolation():
    import routes.trade_routes as tr
    from fastapi import HTTPException
    db = FakeDb()
    tr.get_db = lambda: db
    _run(db.trade_decisions.insert_one(_dec(user_id="u2")))
    _run(db.intraday_candles.insert_one({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M15", "t_basis": "utc", "bars": _bars([100, 121])}))
    with pytest.raises(HTTPException) as ei:
        _run(tr.trade_predictions(None, symbol="../x", limit=20, user={"id": "u1"}))
    assert ei.value.status_code == 422
    out = _run(tr.trade_predictions(None, symbol="XAUUSD", limit=500, user={"id": "u1"}))
    assert out["recent"] == [] and out["calibration"]["decided"] == 0 and out["horizon_bars"] == 96


# ── B ─────────────────────────────────────────────────────────────────────────────────────────────────
def _series(path, spread=0.3):
    """path = list of closes; returns bars with o=prev close and tight wicks."""
    return _bars(path, spread=spread)


def test_b1_double_top_and_bottom():
    import chart_patterns as cp
    base = [100 + (k % 7) * 0.2 for k in range(40)]          # calm warm-up (ATR ~ small)
    path = base + [100, 104, 108, 110, 108, 104, 101, 104, 108, 110.2, 108, 104, 102, 101.5]
    bars = _series(path)
    highs, lows = cp.find_swings(bars)
    a = cp.atr(bars)
    pats = {p["name"]: p for p in cp.detect_patterns(bars, highs, lows, a, len(bars) - 1)}
    dt = pats["double_top"]
    h1, h2 = highs[-2], highs[-1]
    neck, top = min(b["l"] for b in bars[h1["i"]:h2["i"] + 1]), max(h1["p"], h2["p"])
    assert dt["direction"] == "BEARISH" and dt["confirmed"] is False and dt["level"] == neck and dt["invalidation"] == top
    assert abs(dt["target"] - (neck - (top - neck))) < 1e-9
    bars2 = _series(path + [100.5])
    pats2 = {p["name"]: p for p in cp.detect_patterns(bars2, *cp.find_swings(bars2), cp.atr(bars2), len(bars2) - 1)}
    assert pats2["double_top"]["confirmed"] is True
    mirror = _series(base + [100, 96, 92, 90, 92, 96, 99, 96, 92, 89.8, 92, 96, 98, 98.5, 99.5])
    pm = {p["name"]: p for p in cp.detect_patterns(mirror, *cp.find_swings(mirror), cp.atr(mirror), len(mirror) - 1)}
    assert pm["double_bottom"]["direction"] == "BULLISH" and pm["double_bottom"]["confirmed"] is True and 99 < pm["double_bottom"]["level"] < 99.5


def test_b2_head_and_shoulders_and_inverse():
    import chart_patterns as cp
    base = [100 + (k % 5) * 0.1 for k in range(40)]
    hs = _series(base + [100, 104, 106, 104, 101, 106, 112, 106, 101, 104, 106.3, 104, 101, 100.2])
    p = {x["name"]: x for x in cp.detect_patterns(hs, *cp.find_swings(hs), cp.atr(hs), len(hs) - 1)}
    assert p["head_and_shoulders"]["direction"] == "BEARISH" and 100.5 < p["head_and_shoulders"]["level"] < 101 and p["head_and_shoulders"]["confirmed"] is True
    inv = _series(base + [100, 96, 94, 96, 99, 94, 88, 94, 99, 96, 93.7, 96, 99, 99.8])
    p = {x["name"]: x for x in cp.detect_patterns(inv, *cp.find_swings(inv), cp.atr(inv), len(inv) - 1)}
    assert p["inverse_head_and_shoulders"]["direction"] == "BULLISH" and p["inverse_head_and_shoulders"]["confirmed"] is True


def _tri(upper, lower, close_last):
    """Synthetic converging swings: 3 highs on `upper` line, 3 lows on `lower` line."""
    path = [105.0] * 30
    for k in range(3):
        lo, up = lower(k), upper(k)
        path += [lo, (lo + up) / 2, up, (lo + up) / 2]
    path += [(lower(2) + upper(2)) / 2, close_last]
    return _series(path, spread=0.05)


def test_b3_triangles_and_wedges():
    import chart_patterns as cp
    cases = [("ascending_triangle", lambda k: 110.0, lambda k: 100 + 2 * k, 111.0, "BULLISH"),
             ("descending_triangle", lambda k: 110 - 2 * k, lambda k: 100.0, 99.0, "BEARISH"),
             ("rising_wedge", lambda k: 110 + 1 * k, lambda k: 100 + 2.5 * k, 104.0, "BEARISH"),
             ("falling_wedge", lambda k: 110 - 2.5 * k, lambda k: 100 - 1 * k, 108.0, "BULLISH")]
    for name, up, lo, last, direction in cases:
        bars = _tri(up, lo, last)
        p = {x["name"]: x for x in cp.detect_patterns(bars, *cp.find_swings(bars), cp.atr(bars), len(bars) - 1)}
        assert name in p and p[name]["direction"] == direction, name
    sym_up = _tri(lambda k: 110 - 1 * k, lambda k: 100 + 1 * k, 112.0)
    p = {x["name"]: x for x in cp.detect_patterns(sym_up, *cp.find_swings(sym_up), cp.atr(sym_up), len(sym_up) - 1)}
    assert p["symmetrical_triangle"]["direction"] == "BULLISH" and p["symmetrical_triangle"]["confirmed"] is True
    sym_dn = _tri(lambda k: 110 - 1 * k, lambda k: 100 + 1 * k, 98.0)
    p = {x["name"]: x for x in cp.detect_patterns(sym_dn, *cp.find_swings(sym_dn), cp.atr(sym_dn), len(sym_dn) - 1)}
    assert p["symmetrical_triangle"]["direction"] == "BEARISH"


def test_b4_zones_cluster_and_split_at_close():
    import chart_patterns as cp
    bars = _series([100.0] * 10 + [95.0])
    highs = [{"i": 1, "p": 110.0}, {"i": 5, "p": 110.3}, {"i": 9, "p": 120.0}]
    lows = [{"i": 3, "p": 90.0}, {"i": 7, "p": 90.2}]
    z = cp.find_zones(bars, highs, lows, 1.0)
    assert [(x["kind"], x["touches"]) for x in z] == [("support", 2), ("resistance", 2)]      # lone 120 dropped (<2)
    assert z[1]["low"] == 110.0 and z[1]["high"] == 110.3
    assert cp.find_zones(bars, highs, lows, 0) == []


def test_b5_trendline_rejected_when_crossed_then_break_reported():
    import chart_patterns as cp
    bars = _series([100.0] * 20, spread=0.1)
    piv = [{"i": 2, "p": 110.0}, {"i": 8, "p": 110.0}, {"i": 14, "p": 110.0}]
    tl = cp.find_trendline(bars, piv, 1.0, "resistance")
    assert tl and tl["touches"] == 3 and tl["break_i"] is None
    crossed = [dict(b) for b in bars]
    crossed[5]["c"] = 115.0                                               # a close through the line before the last touch
    assert cp.find_trendline(crossed, piv, 1.0, "resistance") is None
    broken = [dict(b) for b in bars]
    broken[17]["c"] = 112.0
    tl = cp.find_trendline(broken, piv, 1.0, "resistance")
    assert tl["break_i"] == 17 and tl["break_dir"] == "BULLISH"


def test_b6_candlesticks():
    import chart_patterns as cp
    prev_down = {"t": 0, "o": 105, "h": 106, "l": 99, "c": 100}
    assert cp.candle_patterns([prev_down, {"t": 1, "o": 99, "h": 107, "l": 98, "c": 106}], 1)[0]["name"] == "bullish_engulfing"
    prev_up = {"t": 0, "o": 100, "h": 106, "l": 99, "c": 105}
    assert cp.candle_patterns([prev_up, {"t": 1, "o": 106, "h": 107, "l": 98, "c": 99}], 1)[0]["name"] == "bearish_engulfing"
    assert cp.candle_patterns([prev_up, {"t": 1, "o": 100, "h": 100.5, "l": 94, "c": 101}], 1)[0]["name"] == "hammer"
    assert cp.candle_patterns([prev_up, {"t": 1, "o": 101, "h": 107, "l": 99.5, "c": 100}], 1)[0]["name"] == "shooting_star"
    assert cp.candle_patterns([prev_up, {"t": 1, "o": 100, "h": 100, "l": 100, "c": 100}], 1) == []


def _long_series(n=800, seed=7):
    import random
    rnd = random.Random(seed)
    c, path = 2000.0, []
    for _ in range(n):
        c += rnd.gauss(0, 2.0) + (3.0 if rnd.random() < 0.05 else 0)
        path.append(c)
    return _series(path, spread=1.5)


def test_b7_no_look_ahead():
    import chart_patterns as cp
    bars = _long_series()
    n = 500
    hi, lo = cp.find_swings(bars[:n])
    a = cp.measure_odds(bars[:n], hi, lo)
    hi2, lo2 = cp.find_swings(bars)
    b = cp.measure_odds(bars, hi2, lo2)
    for name, row in a.items():
        assert b[name]["win"] >= row["win"] and b[name]["loss"] >= row["loss"]      # samples resolved before n are preserved


def test_b8_laplace_odds_and_wilson_lower():
    import chart_patterns as cp
    r = cp._odds_row({"win": 2, "loss": 0, "timeout": 0})
    assert r["odds"] == 0.75 and r["sufficient"] is False and r["lower_bound"] < 1.0
    assert cp.wilson_lower(10, 10) < 1.0


def test_b9_gate_rules():
    import chart_patterns as cp
    snap = {"ready": True, "atr": 1.0, "close": 100.0, "zones": [{"low": 100.2, "high": 100.5, "mid": 100.35, "touches": 3, "kind": "resistance"}], "patterns": []}
    assert "3-touch resistance" in cp.pattern_gate("BUY", snap)
    snap["zones"][0]["touches"] = 2
    assert cp.pattern_gate("BUY", snap) is None
    snap["zones"] = []
    snap["patterns"] = [{"name": "double_top", "direction": "BEARISH", "confirmed": True, "age_bars": 3, "measured": {"win": 15, "samples": 18, "sufficient": True, "lower_bound": 0.62}}]
    assert "double top" in cp.pattern_gate("BUY", snap) and "15/18 hits" in cp.pattern_gate("BUY", snap)
    assert cp.pattern_gate("SELL", snap) is None
    snap["patterns"][0]["measured"]["sufficient"] = False
    assert cp.pattern_gate("BUY", snap) is None
    snap["patterns"][0]["measured"]["sufficient"] = True
    snap["patterns"][0]["age_bars"] = 9
    assert cp.pattern_gate("BUY", snap) is None
    assert cp.pattern_gate("HOLD", snap) is None and cp.pattern_gate("BUY", {"ready": False}) is None


def test_b10_observe_only_wiring():
    br = _read("backend", "bot_runner.py")
    blk = br.split("Spec B — chart-pattern layer")[1].split("# iter-129")[0]
    assert 'signal["chart_patterns"] = {"ready"' in blk and '"would_veto": gd["would_veto"], "enforced": _enforced' in blk
    assert 'if gd["skip"]:' in blk and blk.index('signal["chart_patterns"]') < blk.index('if gd["skip"]:')
    assert blk.count("continue") == 1 and "except Exception" in blk
    assert "_fade_exempt | {\"range_fade\"}" in blk
    assert "pattern_gate_enabled: bool = False" in _read("backend", "models.py")
    bot = _read("backend", "routes", "bot_routes.py")
    assert '"pattern_gate_enabled_changed"' in bot and 'if user.get("role") != "admin":' in bot.split('"pattern_gate_enabled" in update')[1][:400]
    assert '"chart_patterns": chart_pat' in _read("backend", "routes", "posture_routes.py")
    # runtime: a would-veto with the flag off does not skip; on → skips
    import chart_patterns as cp
    snap = {"ready": True, "atr": 1.0, "close": 100.0, "zones": [{"low": 100.2, "high": 100.5, "mid": 100.35, "touches": 3, "kind": "resistance"}], "patterns": []}
    off, on = cp.gate_decision("BUY", snap, False), cp.gate_decision("BUY", snap, True)
    assert off["would_veto"] and not off["skip"] and not off["alert"]
    assert on["skip"] and on["reason"] == on["would_veto"] and not on["alert"]


def test_b11_short_or_garbage_bars_ready_false_and_perf():
    import chart_patterns as cp
    assert cp.pattern_snapshot(_series([100.0] * 20)) == {"ready": False, "reason": "insufficient intraday bars", "bars_n": 20}
    garbage = [{"t": k, "o": "x", "h": None, "l": 1, "c": float("nan")} for k in range(100)]
    assert cp.pattern_snapshot(garbage)["ready"] is False
    assert cp.pattern_snapshot(None)["ready"] is False
    bars = _long_series()
    t = time.perf_counter()
    snap = cp.pattern_snapshot(bars)
    ms = (time.perf_counter() - t) * 1000
    assert snap["ready"] and "odds" in snap and "zones" in snap and all("key" not in p for p in snap["patterns"])
    assert ms < 100, f"snapshot took {ms:.0f} ms"


# ── C ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_c1_list_file_is_skipped_and_valid_one_loads(tmp_path, monkeypatch):
    import json
    import rules_policy as rp
    (tmp_path / "a-list.json").write_text("[1,2,3]")
    (tmp_path / "b-bad-types.json").write_text(json.dumps({"schema": rp.SCHEMA, "policy_version": 5, "issued_at": 42, "expires_at": None, "allowed_brokers": "x"}))
    good = {"policy_version": "ok-v1", "issued_at": "2026-10-01T00:00:00+00:00", "nonce": "n-ok-1"}
    (tmp_path / "c-good.json").write_text(json.dumps(good))
    monkeypatch.setattr(rp, "POLICIES_DIR", str(tmp_path))
    monkeypatch.setattr(rp, "problems", lambda pol, now=None: [] if pol.get("policy_version") == "ok-v1" else (_ for _ in ()).throw(TypeError("boom")))
    activated = []

    async def _activate(db, pol, *, actor, source):
        activated.append(pol["policy_version"])
        return pol
    monkeypatch.setattr(rp, "activate", _activate)
    _run(rp.load_from_repo(FakeDb()))
    assert activated == ["ok-v1"]


def test_c2_future_issued_at_rejected():
    import rules_policy as rp
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    errs = rp.problems({"schema": rp.SCHEMA, "policy_version": "x", "issued_at": (now + timedelta(minutes=10)).isoformat()}, now=now)
    assert "issued_at is in the future" in errs
    errs = rp.problems({"schema": rp.SCHEMA, "policy_version": "x", "issued_at": (now + timedelta(minutes=2)).isoformat()}, now=now)
    assert "issued_at is in the future" not in errs
    assert rp.problems([1, 2]) == ["policy is not an object"]


def test_c3_tombstone_indexes_and_bson_expiry():
    import account_tombstones as at
    db = FakeDb()
    acc = {"_id": ObjectId(), "label": "x", "broker": "b", "account_number": "1", "bridge_token_hash": "h1"}
    doc = _run(at.write_tombstone(db, acc, deleted_by="u"))
    assert isinstance(doc["expires_at"], datetime) and doc["expires_at"].tzinfo is not None and isinstance(doc["expires_at_iso"], str)
    assert doc["expires_at"] > datetime.now(timezone.utc)
    src = _read("backend", "account_tombstones.py")
    assert 'create_index("token_hashes")' in src and 'create_index("expires_at", expireAfterSeconds=0)' in src
    assert "from account_tombstones import ensure_indexes as _tomb_idx" in _read("backend", "server.py")
