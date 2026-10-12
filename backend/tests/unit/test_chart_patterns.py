"""Chart-pattern layer — zones, trendlines, classic + candlestick patterns, measured odds, gate."""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit

import chart_patterns as cp  # noqa: E402
from market_structure import find_swings  # noqa: E402


def _bars_from_path(path, wick=0.3):
    """One bar per path point: open = previous close, small symmetric wicks."""
    out, prev = [], path[0]
    for i, c in enumerate(path):
        o = prev
        out.append({"t": i * 900, "o": o, "c": c, "h": max(o, c) + wick, "l": min(o, c) - wick, "v": 100})
        prev = c
    return out


def _legs(start, *targets, steps=6):
    """Piecewise-linear path through the given turning points."""
    path, cur = [start], start
    for tgt in targets:
        for k in range(1, steps + 1):
            path.append(cur + (tgt - cur) * k / steps)
        cur = tgt
    return path


def _random_walk(n=400, seed=7):
    rnd = random.Random(seed)
    p, path = 100.0, []
    for _ in range(n):
        p += rnd.gauss(0, 1)
        path.append(p)
    return _bars_from_path(path)


def _names(bars):
    highs, lows = find_swings(bars)
    return {p["name"]: p for p in cp.detect_patterns(bars, highs, lows, cp.atr(bars))}


def test_atr_series_matches_point_atr():
    bars = _random_walk(120)
    series = cp.atr_series(bars)
    for t in (5, 20, 60, 119):
        assert series[t] == pytest.approx(cp.atr(bars[:t + 1]))


def test_double_top_detected_and_confirmed_below_neckline():
    bars = _bars_from_path(_legs(100, 110, 104, 110, 100))
    p = _names(bars)["double_top"]
    assert p["direction"] == "BEARISH" and p["confirmed"]
    assert p["level"] == pytest.approx(104 - 0.3, abs=0.5)
    assert p["target"] < p["level"] < p["invalidation"]


def test_double_bottom_mirror():
    bars = _bars_from_path(_legs(110, 100, 106, 100, 110))
    p = _names(bars)["double_bottom"]
    assert p["direction"] == "BULLISH" and p["confirmed"] and p["target"] > p["level"]


def test_double_top_not_confirmed_while_above_neckline():
    bars = _bars_from_path(_legs(100, 110, 104, 110, 106))
    assert not _names(bars)["double_top"]["confirmed"]


def test_head_and_shoulders_and_inverse():
    hs = _names(_bars_from_path(_legs(100, 108, 103, 114, 103, 108, 96)))
    assert hs["head_and_shoulders"]["confirmed"] and hs["head_and_shoulders"]["direction"] == "BEARISH"
    ihs = _names(_bars_from_path(_legs(114, 106, 111, 100, 111, 106, 118)))
    assert ihs["inverse_head_and_shoulders"]["confirmed"]


@pytest.mark.parametrize("turns,name,direction", [
    ((110, 100, 110, 103, 110, 106, 109.5), "ascending_triangle", "BULLISH"),
    ((100, 110, 100, 107, 100, 104, 100.5), "descending_triangle", "BEARISH"),
    ((100, 110, 107, 114, 112, 116, 114.5), "rising_wedge", "BEARISH"),
    ((120, 110, 113, 106, 108, 104, 105.5), "falling_wedge", "BULLISH"),
])
def test_converging_patterns_classified(turns, name, direction):
    found = _names(_bars_from_path(_legs(105, *turns)))
    assert name in found, found.keys()
    assert found[name]["direction"] == direction


def test_zones_cluster_repeated_swings():
    bars = _bars_from_path(_legs(100, 110, 102, 110.2, 101.8, 109.9, 102.1, 106))
    highs, lows = find_swings(bars)
    zones = cp.find_zones(bars, highs, lows, cp.atr(bars))
    res = [z for z in zones if z["kind"] == "resistance"]
    sup = [z for z in zones if z["kind"] == "support"]
    assert res and res[-1]["touches"] == 3 and 109.5 < res[-1]["mid"] < 110.6
    assert sup and sup[0]["touches"] == 3


def test_rising_support_trendline_and_break():
    bars = _bars_from_path(_legs(100, 106, 102, 108, 104, 110, 106, 112, 100))
    highs, lows = find_swings(bars)
    tl = cp.find_trendline(bars, lows, cp.atr(bars), "support")
    assert tl and tl["touches"] >= 3 and tl["slope"] > 0
    assert tl["break_dir"] == "BEARISH" and tl["break_i"] > tl["last_touch_i"]


def test_candlestick_patterns():
    prev = {"o": 101, "c": 100, "h": 101.2, "l": 99.8}
    engulf = {"o": 99.9, "c": 101.5, "h": 101.6, "l": 99.8}
    hammer = {"o": 100.0, "c": 100.2, "h": 100.25, "l": 99.0}
    star = {"o": 100.2, "c": 100.0, "h": 101.2, "l": 99.97}
    assert ("bullish_engulfing", "BULLISH") in cp.candle_patterns([prev, engulf], 1)
    assert ("hammer", "BULLISH") in cp.candle_patterns([prev, hammer], 1)
    assert ("shooting_star", "BEARISH") in cp.candle_patterns([prev, star], 1)


def test_resolve_win_loss_and_same_bar_is_loss():
    bars = [{"h": 101, "l": 99, "c": 100}, {"h": 103, "l": 99.5, "c": 102}, {"h": 100, "l": 97, "c": 98}]
    assert cp._resolve(bars, 0, "BULLISH", 102.5, 98, 5) == "win"
    assert cp._resolve(bars, 0, "BEARISH", 97.5, 102.5, 5) == "loss"
    both = [{"h": 101, "l": 99, "c": 100}, {"h": 105, "l": 95, "c": 100}]
    assert cp._resolve(both, 0, "BULLISH", 103, 97, 5) == "loss"
    assert cp._resolve(bars, 2, "BULLISH", 200, 0, 5) is None          # not enough future bars yet


def test_measured_odds_have_no_lookahead():
    """Odds measured on a prefix must not change when future bars are appended
    (except for outcomes that resolve inside the new bars)."""
    bars = _random_walk(500)
    h, lo = find_swings(bars)
    full = cp.measure_odds(bars, h, lo, horizon=12)
    pre = bars[:300]
    hp, lp = find_swings(pre)
    part = cp.measure_odds(pre, hp, lp, horizon=12)
    for name, s in part.items():
        assert full[name]["win"] >= s["win"] and full[name]["loss"] >= s["loss"]
    for s in full.values():
        assert 0 < s["odds"] < 1 and s["sufficient"] == (s["samples"] >= cp.MIN_SAMPLES)


def test_wilson_lower_bound_penalises_small_samples():
    assert cp.wilson_lower(6, 9) < 0.5 < cp.wilson_lower(30, 40)
    assert 9 < cp.MIN_SAMPLES                     # a lucky 7-of-9 never reaches the gate
    assert cp.wilson_lower(0, 0) == 0.0


def test_snapshot_not_ready_on_short_history():
    assert cp.pattern_snapshot(_random_walk(30))["ready"] is False


def test_snapshot_on_random_walk_is_well_formed():
    snap = cp.pattern_snapshot(_random_walk(800))
    assert snap["ready"] and snap["bars_n"] == 800 and snap["atr"] > 0
    for p in snap["patterns"]:
        assert "key" not in p and p["direction"] in ("BULLISH", "BEARISH", "NEUTRAL")


def _snap(**kw):
    return {"ready": True, "atr": 1.0, "close": 100.0, "zones": [], "patterns": [], **kw}


def test_gate_vetoes_buy_into_strong_resistance_only():
    z = {"low": 100.2, "high": 100.6, "mid": 100.4, "touches": 3, "kind": "resistance"}
    assert "resistance zone" in cp.pattern_gate("BUY", _snap(zones=[z]))
    assert cp.pattern_gate("SELL", _snap(zones=[z])) is None
    assert cp.pattern_gate("BUY", _snap(zones=[{**z, "touches": 2}])) is None
    assert cp.pattern_gate("BUY", _snap(zones=[{**z, "low": 101.0, "high": 101.4}])) is None


def test_gate_vetoes_sell_onto_strong_support():
    z = {"low": 99.5, "high": 99.8, "mid": 99.65, "touches": 4, "kind": "support"}
    assert "support zone" in cp.pattern_gate("SELL", _snap(zones=[z]))


def test_gate_requires_measured_sufficient_odds_and_freshness():
    good = {"win": 18, "samples": 22, "odds": 0.79, "lower_bound": 0.62, "sufficient": True}
    p = {"name": "double_top", "direction": "BEARISH", "confirmed": True, "age_bars": 2, "measured": good}
    assert "double top" in cp.pattern_gate("BUY", _snap(patterns=[p]))
    assert cp.pattern_gate("SELL", _snap(patterns=[p])) is None
    for bad in ({"measured": {**good, "sufficient": False}}, {"measured": {**good, "lower_bound": 0.45}},
                {"measured": None}, {"age_bars": cp.FRESH_BARS + 1}, {"confirmed": False}):
        assert cp.pattern_gate("BUY", _snap(patterns=[{**p, **bad}])) is None


def test_gate_fails_open():
    assert cp.pattern_gate("BUY", {"ready": False}) is None
    assert cp.pattern_gate("BUY", None) is None
    assert cp.pattern_gate("HOLD", _snap()) is None
