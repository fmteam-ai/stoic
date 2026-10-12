"""Spec B — chart-pattern layer with MEASURED odds (observe-only). Pure functions of M15 bars; every detector
fails open (short/garbage data → no opinion). Nothing here places, changes or skips a trade: bot_runner only
skips when the operator set pattern_gate_enabled=true on the bot config."""
from __future__ import annotations

import math

from market_structure import SWING_K, find_swings

ZONE_TOL_ATR, LINE_TOL_ATR, MIN_LINE_TOUCHES, PIVOTS_FOR_LINES = 0.5, 0.25, 3, 8
HORIZON, FRESH_BARS, MIN_SAMPLES, NEAR_ZONE_ATR, MIN_BARS = 24, 8, 12, 0.3, 60
FLAT_PER_BAR_ATR = 0.02


def _tr(b, p):
    return max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) if p else b["h"] - b["l"]


def atr(bars, n=14):
    if len(bars) < 2:
        return 0.0
    trs = [_tr(bars[i], bars[i - 1]) for i in range(max(1, len(bars) - n), len(bars))]
    return sum(trs) / len(trs) if trs else 0.0


def atr_series(bars, n=14):
    out, trs = [0.0], []
    for i in range(1, len(bars)):
        trs.append(_tr(bars[i], bars[i - 1]))
        w = trs[-n:]
        out.append(sum(w) / len(w))
    return out


def find_zones(bars, highs, lows, a):
    if a <= 0 or not bars:
        return []
    pts = sorted([(s["p"], s["i"]) for s in list(highs) + list(lows)])
    clusters, cur = [], []
    for p, i in pts:
        if cur and p - cur[0][0] > ZONE_TOL_ATR * a:
            clusters.append(cur)
            cur = []
        cur.append((p, i))
    if cur:
        clusters.append(cur)
    close = bars[-1]["c"]
    zones = []
    for c in clusters:
        if len(c) < 2:
            continue
        lo, hi = min(p for p, _ in c), max(p for p, _ in c)
        mid = (lo + hi) / 2
        zones.append({"low": lo, "high": hi, "mid": mid, "touches": len(c), "last_i": max(i for _, i in c),
                      "kind": "resistance" if mid > close else "support"})
    return sorted(zones, key=lambda z: z["mid"])


def find_trendline(bars, pivots, a, side):
    piv = list(pivots)[-PIVOTS_FOR_LINES:]
    if len(piv) < MIN_LINE_TOUCHES or a <= 0:
        return None
    tol = LINE_TOL_ATR * a
    best = None
    for xi in range(len(piv)):
        for yi in range(xi + 1, len(piv)):
            x, y = piv[xi], piv[yi]
            if y["i"] == x["i"]:
                continue
            slope = (y["p"] - x["p"]) / (y["i"] - x["i"])
            line = lambda i, x=x, slope=slope: x["p"] + slope * (i - x["i"])  # noqa: E731
            touches = [p for p in piv[xi:] if abs(p["p"] - line(p["i"])) <= tol]
            if len(touches) < MIN_LINE_TOUCHES:
                continue
            last = max(p["i"] for p in touches)
            crossed = any((bars[i]["c"] - line(i) > tol) if side == "resistance" else (line(i) - bars[i]["c"] > tol)
                          for i in range(x["i"], min(last, len(bars) - 1) + 1))
            if crossed:
                continue
            cand = (len(touches), last, x, slope)
            if best is None or cand[:2] > best[:2]:
                best = cand
    if best is None:
        return None
    n, last, x, slope = best
    line = lambda i: x["p"] + slope * (i - x["i"])  # noqa: E731
    break_i = break_dir = None
    for i in range(last + 1, len(bars)):
        if side == "resistance" and bars[i]["c"] - line(i) > tol:
            break_i, break_dir = i, "BULLISH"
            break
        if side == "support" and line(i) - bars[i]["c"] > tol:
            break_i, break_dir = i, "BEARISH"
            break
    return {"side": side, "slope": slope, "touches": n, "value_now": line(len(bars) - 1), "anchor_i": x["i"],
            "last_touch_i": last, "break_i": break_i, "break_dir": break_dir}


def _lsq(points):
    n = len(points)
    mx, my = sum(p["i"] for p in points) / n, sum(p["p"] for p in points) / n
    den = sum((p["i"] - mx) ** 2 for p in points) or 1e-9
    slope = sum((p["i"] - mx) * (p["p"] - my) for p in points) / den
    return slope, my - slope * mx


def _pat(name, direction, key, level, target, inval, confirmed, start_i):
    return {"name": name, "direction": direction, "key": key, "level": level, "target": target, "invalidation": inval,
            "confirmed": bool(confirmed), "start_i": start_i}


def detect_patterns(bars, highs, lows, a, t):
    out = []
    if a <= 0 or t < 1 or t >= len(bars):
        return out
    hs = [s for s in highs if s["i"] + SWING_K <= t]
    ls = [s for s in lows if s["i"] + SWING_K <= t]
    close = bars[t]["c"]
    if len(hs) >= 2:
        h1, h2 = hs[-2], hs[-1]
        between = [b["l"] for b in bars[h1["i"]:h2["i"] + 1]]
        if abs(h1["p"] - h2["p"]) <= ZONE_TOL_ATR * a and between:
            neck, top = min(between), max(h1["p"], h2["p"])
            if top - neck >= a:
                out.append(_pat("double_top", "BEARISH", ("double_top", h1["i"]), neck, neck - (top - neck), top, close < neck, h1["i"]))
    if len(ls) >= 2:
        l1, l2 = ls[-2], ls[-1]
        between = [b["h"] for b in bars[l1["i"]:l2["i"] + 1]]
        if abs(l1["p"] - l2["p"]) <= ZONE_TOL_ATR * a and between:
            neck, bottom = max(between), min(l1["p"], l2["p"])
            if neck - bottom >= a:
                out.append(_pat("double_bottom", "BULLISH", ("double_bottom", l1["i"]), neck, neck + (neck - bottom), bottom, close > neck, l1["i"]))
    if len(hs) >= 3:
        s1, head, s2 = hs[-3], hs[-2], hs[-1]
        if head["p"] > max(s1["p"], s2["p"]) + ZONE_TOL_ATR * a and abs(s1["p"] - s2["p"]) <= a:
            neck = min(b["l"] for b in bars[s1["i"]:s2["i"] + 1])
            out.append(_pat("head_and_shoulders", "BEARISH", ("head_and_shoulders", s1["i"]), neck, neck - (head["p"] - neck), s2["p"], close < neck, s1["i"]))
    if len(ls) >= 3:
        s1, head, s2 = ls[-3], ls[-2], ls[-1]
        if head["p"] < min(s1["p"], s2["p"]) - ZONE_TOL_ATR * a and abs(s1["p"] - s2["p"]) <= a:
            neck = max(b["h"] for b in bars[s1["i"]:s2["i"] + 1])
            out.append(_pat("inverse_head_and_shoulders", "BULLISH", ("inverse_head_and_shoulders", s1["i"]), neck, neck + (neck - head["p"]), s2["p"], close > neck, s1["i"]))
    if len(hs) >= 3 and len(ls) >= 3:
        up, lo = hs[-3:], ls[-3:]
        us, ub = _lsq(up)
        lsl, lb = _lsq(lo)
        start = min(up[0]["i"], lo[0]["i"])
        w0, wt = (us * start + ub) - (lsl * start + lb), (us * t + ub) - (lsl * t + lb)
        if w0 > 0 and 0 < wt < w0:
            flat = FLAT_PER_BAR_ATR * a
            uf, lf = abs(us) <= flat, abs(lsl) <= flat
            name = direction = None
            if uf and lsl > flat:
                name, direction = "ascending_triangle", "BULLISH"
            elif us < -flat and lf:
                name, direction = "descending_triangle", "BEARISH"
            elif us < -flat and lsl > flat:
                name, direction = "symmetrical_triangle", None
            elif us > flat and lsl > flat:
                name, direction = "rising_wedge", "BEARISH"
            elif us < -flat and lsl < -flat:
                name, direction = "falling_wedge", "BULLISH"
            if name:
                upper, lower = us * t + ub, lsl * t + lb
                if direction is None:
                    direction = "BULLISH" if close > upper else "BEARISH" if close < lower else "NEUTRAL"
                level = upper if direction == "BULLISH" else lower
                inval = lower if direction == "BULLISH" else upper
                target = level + w0 if direction == "BULLISH" else level - w0
                confirmed = (close > upper) if direction == "BULLISH" else (close < lower) if direction == "BEARISH" else False
                out.append(_pat(name, direction, (name, start), level, target, inval, confirmed, start))
    return out


def candle_patterns(bars, i):
    if i < 1 or i >= len(bars):
        return []
    b, p = bars[i], bars[i - 1]
    rng = b["h"] - b["l"]
    if rng <= 0:
        return []
    body = abs(b["c"] - b["o"])
    up_w, lo_w = b["h"] - max(b["o"], b["c"]), min(b["o"], b["c"]) - b["l"]
    out = []
    if b["c"] > b["o"] and p["c"] < p["o"] and b["c"] >= p["o"] and b["o"] <= p["c"]:
        out.append({"name": "bullish_engulfing", "direction": "BULLISH"})
    if b["c"] < b["o"] and p["c"] > p["o"] and b["c"] <= p["o"] and b["o"] >= p["c"]:
        out.append({"name": "bearish_engulfing", "direction": "BEARISH"})
    if body > 0 and lo_w >= 2 * body and up_w <= max(body, 0.1 * rng):
        out.append({"name": "hammer", "direction": "BULLISH"})
    if body > 0 and up_w >= 2 * body and lo_w <= max(body, 0.1 * rng):
        out.append({"name": "shooting_star", "direction": "BEARISH"})
    return out


def _resolve(bars, start, direction, target, stop, horizon):
    end = start + horizon
    if end > len(bars):
        return None
    for b in bars[start:end]:
        if direction == "BULLISH":
            if b["l"] <= stop:
                return "loss"
            if b["h"] >= target:
                return "win"
        else:
            if b["h"] >= stop:
                return "loss"
            if b["l"] <= target:
                return "win"
    return "timeout"


def wilson_lower(wins, n, z=1.64):
    if n <= 0:
        return 0.0
    p = wins / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / den
    return round(max(0.0, mid - half), 4)


def _odds_row(stats):
    s = stats["win"] + stats["loss"]
    return {**stats, "samples": s, "odds": round((stats["win"] + 1) / (s + 2), 4), "lower_bound": wilson_lower(stats["win"], s), "sufficient": s >= MIN_SAMPLES}


def measure_odds(bars, highs, lows, horizon=HORIZON, warmup=30):
    ser = atr_series(bars)
    stats, seen = {}, set()
    for t in range(warmup, len(bars)):
        a = ser[t]
        if a <= 0:
            continue
        for p in detect_patterns(bars, highs, lows, a, t):
            if not p["confirmed"] or p["direction"] not in ("BULLISH", "BEARISH") or p["key"] in seen:
                continue
            r = _resolve(bars, t + 1, p["direction"], p["target"], p["invalidation"], horizon)
            if r is None:
                continue
            seen.add(p["key"])
            stats.setdefault(p["name"], {"win": 0, "loss": 0, "timeout": 0})[r] += 1
        close = bars[t]["c"]
        for c in candle_patterns(bars, t):
            sgn = 1 if c["direction"] == "BULLISH" else -1
            r = _resolve(bars, t + 1, c["direction"], close + sgn * a, close - sgn * a, horizon)
            if r is None:
                continue
            stats.setdefault(c["name"], {"win": 0, "loss": 0, "timeout": 0})[r] += 1
    return {k: _odds_row(v) for k, v in stats.items()}


def _clean_bars(bars):
    out = []
    for b in bars or []:
        try:
            o, h, l, c = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])  # noqa: E741
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (o, h, l, c)) or h < l:
            continue
        out.append({"t": b.get("t"), "o": o, "h": h, "l": l, "c": c})
    return out


def pattern_snapshot(bars):
    bars = _clean_bars(bars)
    n = len(bars)
    if n < MIN_BARS:
        return {"ready": False, "reason": "insufficient intraday bars", "bars_n": n}
    highs, lows = find_swings(bars)
    a = atr(bars)
    if a <= 0:
        return {"ready": False, "reason": "flat market (ATR 0)", "bars_n": n}
    t = n - 1
    odds = measure_odds(bars, highs, lows)
    pats = []
    for p in detect_patterns(bars, highs, lows, a, t):
        age = None
        if p["confirmed"]:
            age = 0
            for i in range(t - 1, p["start_i"], -1):
                broken = bars[i]["c"] < p["level"] if p["direction"] == "BEARISH" else bars[i]["c"] > p["level"]
                if not broken:
                    break
                age += 1
        pats.append({k: v for k, v in p.items() if k != "key"} | {"age_bars": age, "measured": odds.get(p["name"])})
    candles = [c | {"measured": odds.get(c["name"])} for c in candle_patterns(bars, t)]
    tl = {s: find_trendline(bars, highs if s == "resistance" else lows, a, s) for s in ("resistance", "support")}
    return {"ready": True, "bars_n": n, "atr": round(a, 6), "close": bars[-1]["c"], "zones": find_zones(bars, highs, lows, a),
            "trendlines": {k: v for k, v in tl.items() if v}, "patterns": pats, "candles": candles, "odds": odds}


def gate_decision(action, snap, enforced, error=None):
    """Spec B + P2-01 (main131). Pure. Observe mode (enforced=False) never skips — an error or not-ready
    snapshot only marks data_unavailable. Enforced mode is a real gate: a would-veto skips, and an error /
    not-ready snapshot ALSO skips (fail closed) and asks for ONE visible alert."""
    snap = snap or {}
    unavailable = bool(error) or not snap.get("ready")
    pg = None if unavailable else pattern_gate(action, snap)
    fail = (f"pattern layer error ({error})" if error
            else (f"pattern data unavailable ({snap.get('reason') or 'snapshot not ready'})" if unavailable else None))
    skip = bool(enforced and (pg or fail))
    reason = pg if (pg and enforced) else (f"pattern gate enforced — {fail}: trade skipped (fail closed)" if skip else None)
    return {"skip": skip, "reason": reason, "would_veto": pg, "data_unavailable": unavailable,
            "alert": bool(skip and fail), "detail": fail}


def pattern_gate(action, snap, min_lower_bound=0.5):
    if action not in ("BUY", "SELL") or not snap or not snap.get("ready") or (snap.get("atr") or 0) <= 0:
        return None
    a, close = snap["atr"], snap["close"]
    for z in snap.get("zones") or []:
        if z["touches"] < 3:
            continue
        if action == "BUY" and z["kind"] == "resistance" and 0 <= z["low"] - close <= NEAR_ZONE_ATR * a:
            return f"Pattern gate: BUY {z['low'] - close:.5g} below a {z['touches']}-touch resistance zone {z['low']:.5g}–{z['high']:.5g} — no room to target."
        if action == "SELL" and z["kind"] == "support" and 0 <= close - z["high"] <= NEAR_ZONE_ATR * a:
            return f"Pattern gate: SELL {close - z['high']:.5g} above a {z['touches']}-touch support zone {z['low']:.5g}–{z['high']:.5g} — no room to target."
    against = "BEARISH" if action == "BUY" else "BULLISH"
    for p in snap.get("patterns") or []:
        m = p.get("measured") or {}
        if (p.get("confirmed") and p.get("direction") == against and p.get("age_bars") is not None and p["age_bars"] <= FRESH_BARS
                and m.get("sufficient") and m.get("lower_bound", 0) > min_lower_bound):
            return (f"Pattern gate: {action} against a confirmed {p['name'].replace('_', ' ')} ({p['age_bars']} bars old) — "
                    f"measured {m['win']}/{m['samples']} hits on this symbol.")
    return None
