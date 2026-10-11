"""Chart-pattern layer — S/R zones, trendlines, classic + candlestick patterns
with MEASURED odds (no hard-coded win rates).

Built on `market_structure.find_swings` over the same M15 bars
({t, o, h, l, c, v}). Everything is a pure function of the bars:

  zones          swing prices clustered within 0.5 × ATR; kept at ≥ 2 touches
  trendlines     best line through swing highs (resistance) / lows (support)
                 with ≥ 3 touches and no close through it before the last touch
  chart patterns double top/bottom, head & shoulders (+ inverse), ascending /
                 descending / symmetrical triangle, rising / falling wedge
  candlesticks   engulfing, hammer, shooting star (confirmation only)
  odds           walk-forward over the same history: every pattern
                 confirmation is scored on whether price reached its target
                 (chart patterns) or +1 ATR (candlesticks) before its
                 invalidation within HORIZON bars. Swings are only used once
                 confirmed (i + k ≤ t) so there is no look-ahead.

`pattern_gate` is the veto. bot_runner runs it in OBSERVE mode unless the
bot config sets `pattern_gate_enabled: true` — the would-veto reason is
recorded on the signal so the gate earns its place in shadow first.
All detectors fail-open on short or missing data.
"""
from __future__ import annotations

from market_structure import SWING_K, find_swings

ZONE_TOL_ATR = 0.5
LINE_TOL_ATR = 0.25
MIN_LINE_TOUCHES = 3
PIVOTS_FOR_LINES = 8
HORIZON = 24                 # bars (6h on M15) for an outcome to resolve
FRESH_BARS = 8               # a confirmation older than this no longer gates
MIN_SAMPLES = 12             # below this the measured odds are "insufficient"
NEAR_ZONE_ATR = 0.3          # entry this close to an opposing zone has no room
MIN_BARS = 60

BULLISH, BEARISH, NEUTRAL = "BULLISH", "BEARISH", "NEUTRAL"


def _true_ranges(bars):
    return [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"]))
            for p, b in zip(bars, bars[1:])]


def atr(bars, n=14) -> float:
    w = _true_ranges(bars)[-n:]
    return sum(w) / len(w) if w else 0.0


def atr_series(bars, n=14) -> list:
    """ATR as known at each bar t (same definition as atr(bars[:t + 1]))."""
    trs, out, run = _true_ranges(bars), [0.0], 0.0
    for j, tr in enumerate(trs):
        run += tr - (trs[j - n] if j >= n else 0.0)
        out.append(run / min(j + 1, n))
    return out


# ───────────────────────────── zones ─────────────────────────────

def find_zones(bars, highs, lows, a: float, tol_atr=ZONE_TOL_ATR) -> list:
    """Cluster swing prices; a zone above the last close is resistance, below is support."""
    if a <= 0 or not bars:
        return []
    pts = sorted([(s["p"], s["i"]) for s in highs + lows])
    clusters, cur = [], []
    for p, i in pts:
        if cur and p - cur[0][0] > tol_atr * a:
            clusters.append(cur)
            cur = []
        cur.append((p, i))
    if cur:
        clusters.append(cur)
    last = bars[-1]["c"]
    out = []
    for c in clusters:
        if len(c) < 2:
            continue
        lo, hi = min(p for p, _ in c), max(p for p, _ in c)
        mid = (lo + hi) / 2
        out.append({"low": lo, "high": hi, "mid": mid, "touches": len(c),
                    "last_i": max(i for _, i in c),
                    "kind": "resistance" if mid > last else "support"})
    return sorted(out, key=lambda z: z["mid"])


# ─────────────────────────── trendlines ───────────────────────────

def _line_at(line, i):
    return line["p1"] + line["slope"] * (i - line["i1"])


def find_trendline(bars, pivots, a: float, side: str, tol_atr=LINE_TOL_ATR):
    """Best line through recent pivots. side='resistance' uses highs, 'support' lows.
    Valid only if no close crossed it between its first anchor and its last touch."""
    piv = pivots[-PIVOTS_FOR_LINES:]
    if a <= 0 or len(piv) < MIN_LINE_TOUCHES:
        return None
    tol = tol_atr * a
    best = None
    for x in range(len(piv)):
        for y in range(x + 1, len(piv)):
            p, q = piv[x], piv[y]
            line = {"i1": p["i"], "p1": p["p"], "slope": (q["p"] - p["p"]) / (q["i"] - p["i"])}
            touches = [s for s in piv[x:] if abs(s["p"] - _line_at(line, s["i"])) <= tol]
            if len(touches) < MIN_LINE_TOUCHES:
                continue
            last_touch = touches[-1]["i"]
            crossed = any(
                (bars[i]["c"] > _line_at(line, i) + tol) if side == "resistance"
                else (bars[i]["c"] < _line_at(line, i) - tol)
                for i in range(p["i"], last_touch + 1))
            if crossed:
                continue
            score = (len(touches), last_touch)
            if best is None or score > best[0]:
                best = (score, {**line, "touches": len(touches), "last_touch_i": last_touch})
    if best is None:
        return None
    line = best[1]
    n = len(bars)
    brk = None
    for i in range(line["last_touch_i"] + 1, n):
        v = _line_at(line, i)
        if (side == "resistance" and bars[i]["c"] > v + tol) or (side == "support" and bars[i]["c"] < v - tol):
            brk = i
            break
    return {"side": side, "slope": line["slope"], "touches": line["touches"],
            "value_now": _line_at(line, n - 1), "anchor_i": line["i1"],
            "last_touch_i": line["last_touch_i"], "break_i": brk,
            "break_dir": (BULLISH if side == "resistance" else BEARISH) if brk is not None else None}


# ───────────────────────── chart patterns ─────────────────────────
# Each detector looks only at the last few confirmed swings and the close at
# bar t, so it can be replayed bar-by-bar for the walk-forward odds.

def _between(lows_or_highs, i0, i1):
    return [s for s in lows_or_highs if i0 < s["i"] < i1]


def _double(highs, lows, close, a, t):
    out = []
    if len(highs) >= 2:
        h1, h2 = highs[-2], highs[-1]
        mids = _between(lows, h1["i"], h2["i"])
        if mids and abs(h1["p"] - h2["p"]) <= ZONE_TOL_ATR * a:
            neck = min(m["p"] for m in mids)
            top = max(h1["p"], h2["p"])
            if top - neck >= a:
                out.append({"name": "double_top", "direction": BEARISH, "key": ("dt", h1["i"], h2["i"]),
                            "level": neck, "target": neck - (top - neck), "invalidation": top,
                            "confirmed": close < neck, "start_i": h1["i"]})
    if len(lows) >= 2:
        l1, l2 = lows[-2], lows[-1]
        mids = _between(highs, l1["i"], l2["i"])
        if mids and abs(l1["p"] - l2["p"]) <= ZONE_TOL_ATR * a:
            neck = max(m["p"] for m in mids)
            bot = min(l1["p"], l2["p"])
            if neck - bot >= a:
                out.append({"name": "double_bottom", "direction": BULLISH, "key": ("db", l1["i"], l2["i"]),
                            "level": neck, "target": neck + (neck - bot), "invalidation": bot,
                            "confirmed": close > neck, "start_i": l1["i"]})
    return out


def _head_shoulders(highs, lows, close, a, t):
    out = []
    if len(highs) >= 3:
        ls, hd, rs = highs[-3:]
        necks = _between(lows, ls["i"], rs["i"])
        if (necks and hd["p"] > max(ls["p"], rs["p"]) + 0.5 * a
                and abs(ls["p"] - rs["p"]) <= a):
            neck = min(n["p"] for n in necks)
            out.append({"name": "head_and_shoulders", "direction": BEARISH, "key": ("hs", ls["i"], rs["i"]),
                        "level": neck, "target": neck - (hd["p"] - neck), "invalidation": rs["p"],
                        "confirmed": close < neck, "start_i": ls["i"]})
    if len(lows) >= 3:
        ls, hd, rs = lows[-3:]
        necks = _between(highs, ls["i"], rs["i"])
        if (necks and hd["p"] < min(ls["p"], rs["p"]) - 0.5 * a
                and abs(ls["p"] - rs["p"]) <= a):
            neck = max(n["p"] for n in necks)
            out.append({"name": "inverse_head_and_shoulders", "direction": BULLISH, "key": ("ihs", ls["i"], rs["i"]),
                        "level": neck, "target": neck + (neck - hd["p"]), "invalidation": rs["p"],
                        "confirmed": close > neck, "start_i": ls["i"]})
    return out


def _fit(points):
    n = len(points)
    mx = sum(p["i"] for p in points) / n
    my = sum(p["p"] for p in points) / n
    den = sum((p["i"] - mx) ** 2 for p in points) or 1.0
    slope = sum((p["i"] - mx) * (p["p"] - my) for p in points) / den
    return slope, my - slope * mx


def _converging(highs, lows, close, a, t):
    """Triangles and wedges from least-squares lines through the last 3 highs and 3 lows."""
    if len(highs) < 3 or len(lows) < 3:
        return []
    hs, ls = highs[-3:], lows[-3:]
    su, iu = _fit(hs)
    sl, il = _fit(ls)
    up, lo = su * t + iu, sl * t + il
    width_start = (su * min(hs[0]["i"], ls[0]["i"]) + iu) - (sl * min(hs[0]["i"], ls[0]["i"]) + il)
    if up <= lo or width_start <= 0 or (up - lo) >= width_start:
        return []                                    # not converging
    flat = 0.02 * a                                  # per-bar slope that counts as horizontal
    if abs(su) <= flat and sl > flat:
        name, d = "ascending_triangle", BULLISH
    elif su < -flat and abs(sl) <= flat:
        name, d = "descending_triangle", BEARISH
    elif su < -flat and sl > flat:
        name, d = "symmetrical_triangle", NEUTRAL
    elif su > flat and sl > flat:
        name, d = "rising_wedge", BEARISH
    elif su < -flat and sl < -flat:
        name, d = "falling_wedge", BULLISH
    else:
        return []
    height = width_start
    start = min(hs[0]["i"], ls[0]["i"])
    key = (name, start)
    if close > up:
        broke = BULLISH
    elif close < lo:
        broke = BEARISH
    else:
        broke = None
    # Only a break in the pattern's own direction confirms it; a symmetrical
    # triangle confirms whichever way it breaks.
    direction = broke if d == NEUTRAL and broke else d
    confirmed = broke is not None and broke == direction
    level = up if direction == BULLISH else lo
    sign = 1 if direction == BULLISH else -1
    return [{"name": name, "direction": direction, "key": key, "level": level,
             "target": level + sign * height,
             "invalidation": lo if direction == BULLISH else up,
             "confirmed": confirmed, "start_i": start}]


DETECTORS = (_double, _head_shoulders, _converging)


def detect_patterns(bars, highs, lows, a: float, t: int | None = None) -> list:
    """Patterns visible at bar t using swings confirmed by then (default: last bar)."""
    t = len(bars) - 1 if t is None else t
    if a <= 0 or t < 0:
        return []
    hk = [s for s in highs if s["i"] + SWING_K <= t]
    lk = [s for s in lows if s["i"] + SWING_K <= t]
    close = bars[t]["c"]
    out = []
    for det in DETECTORS:
        out.extend(det(hk, lk, close, a, t))
    return out


# ─────────────────────────── candlesticks ───────────────────────────

def candle_patterns(bars, i: int) -> list:
    if i < 1:
        return []
    b, p = bars[i], bars[i - 1]
    body = abs(b["c"] - b["o"])
    rng = b["h"] - b["l"]
    if rng <= 0:
        return []
    upper = b["h"] - max(b["o"], b["c"])
    lower = min(b["o"], b["c"]) - b["l"]
    out = []
    if b["c"] > b["o"] and p["c"] < p["o"] and b["c"] >= p["o"] and b["o"] <= p["c"]:
        out.append(("bullish_engulfing", BULLISH))
    if b["c"] < b["o"] and p["c"] > p["o"] and b["c"] <= p["o"] and b["o"] >= p["c"]:
        out.append(("bearish_engulfing", BEARISH))
    if lower >= 2 * body and upper <= max(body, 0.1 * rng) and body > 0:
        out.append(("hammer", BULLISH))
    if upper >= 2 * body and lower <= max(body, 0.1 * rng) and body > 0:
        out.append(("shooting_star", BEARISH))
    return out


# ───────────────────────── measured odds ─────────────────────────

def _resolve(bars, t, direction, target, stop, horizon):
    """'win' if target is hit before stop within horizon bars, else 'loss' / 'timeout'."""
    for b in bars[t + 1:t + 1 + horizon]:
        hit_stop = b["l"] <= stop if direction == BULLISH else b["h"] >= stop
        hit_tgt = b["h"] >= target if direction == BULLISH else b["l"] <= target
        if hit_stop:                     # same bar: assume the worse outcome
            return "loss"
        if hit_tgt:
            return "win"
    return "timeout" if t + horizon < len(bars) else None


def wilson_lower(wins: int, n: int, z: float = 1.64) -> float:
    """One-sided 95% lower bound on the true hit rate (Wilson score interval)."""
    if n <= 0:
        return 0.0
    ph = wins / n
    den = 1 + z * z / n
    return (ph + z * z / (2 * n) - z * ((ph * (1 - ph) + z * z / (4 * n)) / n) ** 0.5) / den


def measure_odds(bars, highs, lows, horizon=HORIZON, warmup=30) -> dict:
    """Walk-forward success rates per pattern name over this history."""
    n = len(bars)
    stats: dict = {}
    seen = set()
    atrs = atr_series(bars)
    for t in range(warmup, n):
        a = atrs[t]
        if a <= 0:
            continue
        for p in detect_patterns(bars, highs, lows, a, t):
            if not p["confirmed"] or p["key"] in seen or p["direction"] == NEUTRAL:
                continue
            seen.add(p["key"])
            res = _resolve(bars, t, p["direction"], p["target"], p["invalidation"], horizon)
            if res:
                stats.setdefault(p["name"], {"win": 0, "loss": 0, "timeout": 0})[res] += 1
        c = bars[t]["c"]
        for name, d in candle_patterns(bars, t):
            sign = 1 if d == BULLISH else -1
            res = _resolve(bars, t, d, c + sign * a, c - sign * a, horizon)
            if res:
                stats.setdefault(name, {"win": 0, "loss": 0, "timeout": 0})[res] += 1
    out = {}
    for name, s in stats.items():
        decided = s["win"] + s["loss"]
        out[name] = {**s, "samples": decided,
                     # Laplace-smoothed so 2/2 does not read as 100%
                     "odds": round((s["win"] + 1) / (decided + 2), 4),
                     "lower_bound": round(wilson_lower(s["win"], decided), 4),
                     "sufficient": decided >= MIN_SAMPLES}
    return out


# ───────────────────────────── snapshot ─────────────────────────────

def pattern_snapshot(bars, with_odds: bool = True) -> dict:
    if not bars or len(bars) < MIN_BARS:
        return {"ready": False, "reason": "insufficient intraday bars", "bars_n": len(bars or [])}
    highs, lows = find_swings(bars)
    a = atr(bars)
    n = len(bars)
    odds = measure_odds(bars, highs, lows) if with_odds else {}
    patterns = []
    for p in detect_patterns(bars, highs, lows, a):
        conf_age = None
        if p["confirmed"]:
            # bars since the confirming close first crossed the level
            conf_age = 0
            for i in range(n - 2, p["start_i"], -1):
                c = bars[i]["c"]
                still = c > p["level"] if p["direction"] == BULLISH else c < p["level"]
                if not still:
                    break
                conf_age += 1
        patterns.append({k: v for k, v in p.items() if k != "key"} |
                        {"age_bars": conf_age, "measured": odds.get(p["name"])})
    candles = [{"name": nm, "direction": d, "measured": odds.get(nm)} for nm, d in candle_patterns(bars, n - 1)]
    return {
        "ready": True, "bars_n": n, "atr": a, "close": bars[-1]["c"],
        "zones": find_zones(bars, highs, lows, a),
        "trendlines": [tl for tl in (find_trendline(bars, highs, a, "resistance"),
                                     find_trendline(bars, lows, a, "support")) if tl],
        "patterns": patterns, "candles": candles, "odds": odds,
    }


def pattern_gate(action: str, snap: dict, min_lower_bound: float = 0.5) -> str | None:
    """Veto entries with no room into a strong opposing zone, or against a fresh
    confirmed pattern whose MEASURED hit rate on this symbol beats a coin flip
    with 95% confidence (a lucky 7-of-9 does not qualify)."""
    if action not in ("BUY", "SELL") or not snap or not snap.get("ready"):
        return None
    a, close = snap["atr"], snap["close"]
    if a <= 0:
        return None
    for z in snap.get("zones") or []:
        if z["touches"] < 3:
            continue
        if action == "BUY" and z["kind"] == "resistance" and 0 <= z["low"] - close <= NEAR_ZONE_ATR * a:
            return (f"Pattern gate: BUY {z['low'] - close:.5g} below a {z['touches']}-touch "
                    f"resistance zone {z['low']:.5g}–{z['high']:.5g} — no room to target.")
        if action == "SELL" and z["kind"] == "support" and 0 <= close - z["high"] <= NEAR_ZONE_ATR * a:
            return (f"Pattern gate: SELL {close - z['high']:.5g} above a {z['touches']}-touch "
                    f"support zone {z['low']:.5g}–{z['high']:.5g} — no room to target.")
    against = BEARISH if action == "BUY" else BULLISH
    for p in snap.get("patterns") or []:
        m = p.get("measured") or {}
        if (p["confirmed"] and p["direction"] == against and (p.get("age_bars") or 0) <= FRESH_BARS
                and m.get("sufficient") and m.get("lower_bound", 0) > min_lower_bound):
            return (f"Pattern gate: {action} against a confirmed {p['name'].replace('_', ' ')} "
                    f"({p['age_bars']} bars ago; measured {m['win']}/{m['samples']} hits on this "
                    f"symbol, ≥{m['lower_bound']:.0%} at 95% confidence).")
    return None
