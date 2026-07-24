"""Autopilot #12 — composite trend-quality score + exhaustion detector.

Never a single MA crossover: the score blends structure alignment, momentum
persistence, volatility support, cross-timeframe agreement and spread
suitability into 0-100 per direction, and flags trend exhaustion (momentum
divergence, weakening follow-through, rejection wicks, failed breakout,
abnormal acceleration). Informational: stamped on signals and served via
/api/bot/trend-score; hard enforcement stays with the existing gates.
"""


def _ema(vals, n):
    if not vals:
        return []
    k = 2.0 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _direction(closes):
    e8, e20 = _ema(closes, 8)[-1], _ema(closes, 20)[-1]
    gap = (e8 - e20) / e20 * 100 if e20 else 0.0
    if gap >= 0.05:
        return "UP", gap
    if gap <= -0.05:
        return "DOWN", gap
    return "FLAT", gap


def trend_quality(bars: list, spread_pips: float | None = None,
                  symbol: str | None = None) -> dict:
    closes = [b["c"] for b in bars]
    direction, gap = _direction(closes)
    up = direction == "UP"
    comps = {}

    # structure alignment — rising/falling highs AND lows over 3 windows
    win = max(6, len(bars) // 6)
    segs = [bars[-3 * win:-2 * win], bars[-2 * win:-win], bars[-win:]]
    highs = [max(b["h"] for b in s) for s in segs if s]
    lows = [min(b["l"] for b in s) for s in segs if s]
    if len(highs) == 3:
        hh = highs[0] < highs[1] < highs[2]
        hl = lows[0] < lows[1] < lows[2]
        lh = highs[0] > highs[1] > highs[2]
        ll = lows[0] > lows[1] > lows[2]
        aligned = (hh and hl) if up else (lh and ll)
        partial = (hh or hl) if up else (lh or ll)
        comps["structure_alignment"] = 100 if aligned else 55 if partial else 15
    else:
        comps["structure_alignment"] = 50

    # momentum persistence — with-trend closes over the last 12 bars
    last = bars[-12:]
    withs = sum(1 for i in range(1, len(last))
                if (last[i]["c"] > last[i - 1]["c"]) == up)
    comps["momentum_persistence"] = round(withs / max(1, len(last) - 1) * 100)

    # volatility support — with-trend bars carry the bigger ranges
    w_r, c_r = [], []
    for i in range(1, len(bars[-24:])):
        b, p = bars[-24:][i], bars[-24:][i - 1]
        (w_r if (b["c"] > p["c"]) == up else c_r).append(b["h"] - b["l"])
    if w_r and c_r and sum(c_r):
        ratio = (sum(w_r) / len(w_r)) / (sum(c_r) / len(c_r))
        comps["volatility_support"] = round(min(100, max(0, ratio * 50)))
    else:
        comps["volatility_support"] = 50

    # cross-timeframe agreement — 1H resample EMA slope agrees with M15
    h1 = [bars[i]["c"] for i in range(len(bars) - 1, -1, -4)][::-1]
    if len(h1) >= 10:
        d1, _ = _direction(h1)
        comps["cross_timeframe"] = (100 if d1 == direction
                                    else 50 if d1 == "FLAT" else 0)
    else:
        comps["cross_timeframe"] = 50

    # spread suitability — expensive market degrades the score
    from adaptive_sizing import spread_mult
    sm = spread_mult(spread_pips, symbol)
    comps["spread_suitability"] = round(max(0, (sm - 0.55) / 0.45 * 100))

    weights = {"structure_alignment": 0.25, "momentum_persistence": 0.25,
               "volatility_support": 0.15, "cross_timeframe": 0.20,
               "spread_suitability": 0.15}
    score = round(sum(comps[k] * w for k, w in weights.items()))
    if direction == "FLAT":
        score = min(score, 40)
    return {"direction": direction, "score": score,
            "ema_gap_pct": round(gap, 3), "components": comps}


def exhaustion_signals(bars: list, direction: str) -> dict:
    """Each fired signal adds 20 points; ≥60 = trend likely exhausted."""
    if direction not in ("UP", "DOWN") or len(bars) < 30:
        return {"score": 0, "signals": []}
    up = direction == "UP"
    fired = []
    ranges = sorted(b["h"] - b["l"] for b in bars[-25:])
    med = ranges[len(ranges) // 2] or 1e-9

    # momentum divergence — new extreme but the push is fading
    ext_now = max(b["h"] for b in bars[-3:]) if up else min(b["l"] for b in bars[-3:])
    ext_prev = max(b["h"] for b in bars[-20:-3]) if up else min(b["l"] for b in bars[-20:-3])
    push_now = abs(bars[-1]["c"] - bars[-6]["c"])
    push_prev = abs(bars[-6]["c"] - bars[-11]["c"])
    new_extreme = ext_now > ext_prev if up else ext_now < ext_prev
    if new_extreme and push_prev > 0 and push_now < 0.4 * push_prev:
        fired.append("momentum_divergence")

    # weakening follow-through — bodies shrinking hard
    body = lambda b: abs(b["c"] - b["o"]) if "o" in b else abs(b["h"] - b["l"]) * 0.5  # noqa: E731
    recent = sum(body(b) for b in bars[-4:]) / 4
    prior = sum(body(b) for b in bars[-12:-4]) / 8
    if prior > 0 and recent < 0.5 * prior:
        fired.append("weakening_follow_through")

    # rejection wicks — opposing wicks dominate at the extreme
    opp = []
    for b in bars[-4:]:
        rng = (b["h"] - b["l"]) or 1e-9
        wick = (b["h"] - max(b["c"], b.get("o", b["c"]))) if up \
            else (min(b["c"], b.get("o", b["c"])) - b["l"])
        opp.append(wick / rng)
    if sum(opp) / len(opp) >= 0.5:
        fired.append("rejection_wicks")

    # failed breakout — closed beyond the 20-bar channel then back inside
    hi = max(b["h"] for b in bars[-26:-6])
    lo = min(b["l"] for b in bars[-26:-6])
    broke = back = False
    for b in bars[-6:]:
        if (b["c"] > hi if up else b["c"] < lo):
            broke = True
        elif broke and (b["c"] < hi if up else b["c"] > lo):
            back = True
    if broke and back:
        fired.append("failed_breakout")

    # abnormal acceleration — blow-off bar in trend direction
    lastb = bars[-1]
    if (lastb["h"] - lastb["l"]) >= 2.5 * med and \
            ((lastb["c"] > bars[-2]["c"]) == up):
        fired.append("abnormal_acceleration")

    return {"score": min(100, len(fired) * 20), "signals": fired}


def trend_report(bars: list, spread_pips: float | None = None,
                 symbol: str | None = None) -> dict:
    q = trend_quality(bars, spread_pips, symbol)
    ex = exhaustion_signals(bars, q["direction"])
    return {"quality": q, "exhaustion": ex,
            "exhausted": ex["score"] >= 60}
