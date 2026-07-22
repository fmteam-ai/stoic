"""Phase C · Regime classifier — deterministic market-state classification
from accumulated M15 candles, with an H1 multi-timeframe confirmation.

Classes: TREND_UP | TREND_DOWN | RANGE | VOLATILITY_SHOCK (+ confidence 0-1).
Pure function — no DB, no I/O; the permissions slow path feeds it bars.
"""

SHOCK_RANGE_PIPS = 60.0        # 8-bar (2h) range beyond this = shock
TREND_SLOPE_PIPS = 2.0         # EMA10 drift over ~45min to call a trend
ER_TREND_FLOOR = 0.30          # efficiency ratio below this = chop


def _ema(values: list, period: int) -> list:
    k = 2.0 / (period + 1)
    out = []
    ema = values[0]
    for v in values:
        ema += k * (v - ema)
        out.append(ema)
    return out


def classify(bars_m15: list, pip_size: float,
             shock_range_pips: float = SHOCK_RANGE_PIPS) -> dict:
    """bars_m15: chronological [{c: close, ...}] — needs ≥12, uses ≤24."""
    closes = [float(b["c"]) for b in bars_m15][-24:]
    if len(closes) < 12:
        return {"regime": "UNKNOWN", "confidence": 0.0,
                "reason": "insufficient M15 context"}
    pip = pip_size
    emas = _ema(closes, 10)
    slope_pips = (emas[-1] - emas[-4]) / pip           # ~45 min of EMA drift
    rng_pips = (max(closes[-8:]) - min(closes[-8:])) / pip

    # Kaufman efficiency ratio over the last 12 bars: 1 = straight line,
    # ~0 = pure chop. Separates real trends from noisy drift.
    net = abs(closes[-1] - closes[-12])
    path = sum(abs(closes[i] - closes[i - 1])
               for i in range(len(closes) - 11, len(closes)))
    er = (net / path) if path > 0 else 0.0

    # H1 confirmation: resample M15 → H1 closes, EMA5 slope sign agreement
    h1 = closes[3::4] if len(closes) >= 16 else closes[::4]
    h1_slope = 0.0
    if len(h1) >= 4:
        h1_emas = _ema(h1, 5)
        h1_slope = (h1_emas[-1] - h1_emas[-2]) / pip
    h1_agrees = (h1_slope > 0) == (slope_pips > 0) and abs(h1_slope) > 0.5

    if rng_pips > shock_range_pips:
        return {"regime": "VOLATILITY_SHOCK",
                "confidence": min(1.0, rng_pips / (2 * shock_range_pips)),
                "ema_slope_pips": round(slope_pips, 2),
                "range_pips": round(rng_pips, 1),
                "efficiency_ratio": round(er, 3), "h1_agrees": h1_agrees}
    if abs(slope_pips) >= TREND_SLOPE_PIPS and er >= ER_TREND_FLOOR:
        conf = min(1.0, abs(slope_pips) / (2 * TREND_SLOPE_PIPS)) \
            * (0.6 + 0.4 * min(1.0, er / 0.6))
        if h1_agrees:
            conf = min(1.0, conf * 1.15)
        return {"regime": "TREND_UP" if slope_pips > 0 else "TREND_DOWN",
                "confidence": round(conf, 3),
                "ema_slope_pips": round(slope_pips, 2),
                "range_pips": round(rng_pips, 1),
                "efficiency_ratio": round(er, 3), "h1_agrees": h1_agrees}
    return {"regime": "RANGE",
            "confidence": round(min(1.0, (1.0 - er) *
                                    (1.0 if abs(slope_pips) < 1.0 else 0.7)),
                                3),
            "ema_slope_pips": round(slope_pips, 2),
            "range_pips": round(rng_pips, 1),
            "efficiency_ratio": round(er, 3), "h1_agrees": h1_agrees}
