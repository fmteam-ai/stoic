"""iter-60 · Market Structure Agent — SMC detectors on M15 candles.

Candles are streamed by EA v1.42 (`POST /api/bridge/candles`) into the
`intraday_candles` collection as {t, o, h, l, c, v} dicts (broker epoch).

Detectors: swing points → break of structure (BOS), liquidity sweeps,
fair value gaps (FVG), accumulation/distribution. `structure_gate` vetoes
trades against fresh structure (e.g. SELL right after a bullish BOS)."""

SWING_K = 2            # fractal strength (bars each side)
BOS_FRESH_BARS = 16    # 4h of M15 — how long a BOS dominates bias
SWEEP_LOOKBACK = 20
SWEEP_FRESH_BARS = 8


def find_swings(bars, k=SWING_K):
    highs, lows = [], []
    for i in range(k, len(bars) - k):
        h, l = bars[i]["h"], bars[i]["l"]
        if all(h > bars[j]["h"] for j in range(i - k, i)) and \
           all(h >= bars[j]["h"] for j in range(i + 1, i + k + 1)):
            highs.append({"i": i, "p": h})
        if all(l < bars[j]["l"] for j in range(i - k, i)) and \
           all(l <= bars[j]["l"] for j in range(i + 1, i + k + 1)):
            lows.append({"i": i, "p": l})
    return highs, lows


def detect_bos(bars, highs, lows, k=SWING_K):
    """A BOS fires when a close crosses the most recent confirmed,
    still-unbroken swing high (bullish) or swing low (bearish)."""
    events = []
    active_high = active_low = None
    hi = lo = 0
    for i in range(len(bars)):
        while hi < len(highs) and highs[hi]["i"] + k == i:
            active_high = highs[hi]; hi += 1
        while lo < len(lows) and lows[lo]["i"] + k == i:
            active_low = lows[lo]; lo += 1
        c = bars[i]["c"]
        if active_high and c > active_high["p"]:
            events.append({"dir": "BULLISH", "i": i, "level": active_high["p"]})
            active_high = None
        if active_low and c < active_low["p"]:
            events.append({"dir": "BEARISH", "i": i, "level": active_low["p"]})
            active_low = None
    return events


def detect_sweeps(bars, highs, lows, k=SWING_K, lookback=SWEEP_LOOKBACK):
    """Liquidity sweep = wick takes out a swing level but the bar closes back
    inside (stop hunt). BUY_SIDE = above a swing high; SELL_SIDE = below a low."""
    sweeps = []
    for s in highs:
        for i in range(s["i"] + k, min(s["i"] + k + lookback, len(bars))):
            b = bars[i]
            if b["c"] > s["p"]:
                break  # genuine break, not a sweep
            if b["h"] > s["p"]:
                sweeps.append({"side": "BUY_SIDE", "i": i, "level": s["p"]})
                break
    for s in lows:
        for i in range(s["i"] + k, min(s["i"] + k + lookback, len(bars))):
            b = bars[i]
            if b["c"] < s["p"]:
                break
            if b["l"] < s["p"]:
                sweeps.append({"side": "SELL_SIDE", "i": i, "level": s["p"]})
                break
    sweeps.sort(key=lambda x: x["i"])
    return sweeps


def detect_fvg(bars):
    """3-candle fair value gaps, unfilled by any later bar."""
    gaps = []
    for i in range(2, len(bars)):
        a, c = bars[i - 2], bars[i]
        if c["l"] > a["h"]:
            gaps.append({"dir": "BULLISH", "top": c["l"], "bottom": a["h"], "i": i})
        elif c["h"] < a["l"]:
            gaps.append({"dir": "BEARISH", "top": a["l"], "bottom": c["h"], "i": i})
    out = []
    for g in gaps:
        filled = False
        for b in bars[g["i"] + 1:]:
            if g["dir"] == "BULLISH" and b["l"] <= g["bottom"]:
                filled = True; break
            if g["dir"] == "BEARISH" and b["h"] >= g["top"]:
                filled = True; break
        if not filled:
            out.append(g)
    return out


def acc_dist(bars, window=40):
    """Accumulation/Distribution line (CLV × volume) over the last `window`
    bars — phase from the normalised slope of the line."""
    w = bars[-window:]
    if len(w) < 10:
        return {"phase": "UNKNOWN", "slope": 0.0}
    ad, line = 0.0, []
    for b in w:
        rng = b["h"] - b["l"]
        clv = 0.0 if rng <= 0 else ((b["c"] - b["l"]) - (b["h"] - b["c"])) / rng
        ad += clv * float(b.get("v") or 1)
        line.append(ad)
    n = len(line)
    first = sum(line[:n // 2]) / (n // 2)
    second = sum(line[n // 2:]) / (n - n // 2)
    scale = max(abs(v) for v in line) or 1.0
    norm = (second - first) / scale
    phase = ("ACCUMULATION" if norm > 0.15
             else "DISTRIBUTION" if norm < -0.15 else "NEUTRAL")
    return {"phase": phase, "slope": round(norm, 3)}


def structure_snapshot(bars) -> dict:
    if not bars or len(bars) < 30:
        return {"ready": False, "reason": "insufficient intraday bars",
                "bars_n": len(bars or [])}
    highs, lows = find_swings(bars)
    bos = detect_bos(bars, highs, lows)
    sweeps = detect_sweeps(bars, highs, lows)
    fvg = detect_fvg(bars)[-5:]
    n = len(bars)
    last_bos = None
    bias = "NEUTRAL"
    if bos:
        e = bos[-1]
        last_bos = {**e, "age_bars": n - 1 - e["i"]}
        if last_bos["age_bars"] <= BOS_FRESH_BARS:
            bias = e["dir"]
    recent_sweep = None
    if sweeps and n - 1 - sweeps[-1]["i"] <= SWEEP_FRESH_BARS:
        recent_sweep = {**sweeps[-1], "age_bars": n - 1 - sweeps[-1]["i"]}
    return {"ready": True, "bias": bias, "last_bos": last_bos,
            "recent_sweep": recent_sweep, "unfilled_fvg": fvg,
            "acc_dist": acc_dist(bars), "bars_n": n}


def structure_gate(action: str, snapshot: dict) -> str | None:
    """Veto trades against fresh M15 structure."""
    if action not in ("BUY", "SELL") or not snapshot or not snapshot.get("ready"):
        return None
    bias = snapshot.get("bias")
    lb = snapshot.get("last_bos") or {}
    if action == "SELL" and bias == "BULLISH":
        return (f"Structure gate: bullish break of structure {lb.get('age_bars', '?')} "
                f"M15-bars ago at {lb.get('level')} — SELL against fresh bullish "
                f"structure vetoed.")
    if action == "BUY" and bias == "BEARISH":
        return (f"Structure gate: bearish break of structure {lb.get('age_bars', '?')} "
                f"M15-bars ago at {lb.get('level')} — BUY against fresh bearish "
                f"structure vetoed.")
    return None
