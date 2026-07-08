"""Multi-Timeframe Confluence engine (iter-125) — user-specified strategy.

Top-down cascade, all derived from the EA's M15 stream + live quote:
  4H  → overall trend        (resampled 16×M15)
  1H  → intermediate structure (resampled 4×M15)
  15M → setup (pullback toward EMA20 inside the higher-TF trend)
  entry → live-price breakout of the pullback's minor swing level
          (tick-precision via the live quote; the EA does not stream M5)

Only trades when 4H and 1H agree, M15 shows a pullback, and price breaks
the swing level in the trend direction.
"""
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("mtf-confluence")

DETERMINISTIC_SCOPES = ("range_scalp", "mtf_confluence")
PULLBACK_MIN_RETRACE = 0.25
PULLBACK_MAX_RETRACE = 0.70
IMPULSE_MIN_ATR = 2.0        # trend leg must be ≥ this × ATR15 to matter


def _ema(values: list, period: int) -> float:
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def resample(bars: list, factor: int) -> list:
    """Group M15 bars into higher-TF bars (factor 4 = 1H, 16 = 4H)."""
    out = {}
    for b in bars:
        key = int(b["t"]) // (900 * factor)
        if key not in out:
            out[key] = {"t": key * 900 * factor, "o": b["o"], "h": b["h"],
                        "l": b["l"], "c": b["c"], "v": b.get("v") or 0}
        else:
            g = out[key]
            g["h"] = max(g["h"], b["h"])
            g["l"] = min(g["l"], b["l"])
            g["c"] = b["c"]
            g["v"] += b.get("v") or 0
    return [out[k] for k in sorted(out)]


def tf_trend(bars: list) -> str:
    """UP/DOWN/FLAT for a bar series. EMA8 vs EMA20 when enough data,
    otherwise regression-style net-change fallback for young series."""
    closes = [float(b["c"]) for b in bars]
    if len(closes) < 4:
        return "FLAT"
    if len(closes) >= 20:
        fast, slow = _ema(closes[-20:], 8), _ema(closes, 20)
        gap = (fast - slow) / slow if slow else 0
        # Trend = EMA stack separation only. Price vs fast EMA is deliberately
        # ignored — during a pullback price dips below the fast EMA and that
        # is exactly when the M15 setup layer needs the trend to stay valid.
        if gap > 0.002:
            return "UP"
        if gap < -0.002:
            return "DOWN"
        return "FLAT"
    # young series: net change over available bars vs its own noise
    net = closes[-1] - closes[0]
    rng = max(float(b["h"]) for b in bars) - min(float(b["l"]) for b in bars)
    if rng <= 0:
        return "FLAT"
    if net / rng > 0.35:
        return "UP"
    if net / rng < -0.35:
        return "DOWN"
    return "FLAT"


def detect_pullback(m15: list, direction: str, atr15: float) -> dict:
    """Impulse leg in `direction` + a 25-70% retrace = setup. Returns the
    minor swing level whose break confirms trend resumption."""
    out = {"ready": False, "swing_level": None, "note": ""}
    if len(m15) < 20 or atr15 <= 0:
        out["note"] = "insufficient M15 data"
        return out
    closes = [float(b["c"]) for b in m15]
    seg = m15[-24:]
    if direction == "UP":
        leg_low = min(float(b["l"]) for b in seg)
        leg_high = max(float(b["h"]) for b in seg)
        leg = leg_high - leg_low
        if leg < IMPULSE_MIN_ATR * atr15:
            out["note"] = f"no impulse leg (range {leg:.1f} < {IMPULSE_MIN_ATR}×ATR)"
            return out
        retrace = (leg_high - closes[-1]) / leg
        if not (PULLBACK_MIN_RETRACE <= retrace <= PULLBACK_MAX_RETRACE):
            out["note"] = f"retrace {retrace:.0%} outside {PULLBACK_MIN_RETRACE:.0%}-{PULLBACK_MAX_RETRACE:.0%}"
            return out
        swing = max(float(b["h"]) for b in m15[-3:])
        out.update(ready=True, swing_level=round(swing, 2),
                   note=f"pullback {retrace:.0%} of {leg:.1f}pt leg — break {swing:.2f} to resume UP")
    else:
        leg_low = min(float(b["l"]) for b in seg)
        leg_high = max(float(b["h"]) for b in seg)
        leg = leg_high - leg_low
        if leg < IMPULSE_MIN_ATR * atr15:
            out["note"] = f"no impulse leg (range {leg:.1f} < {IMPULSE_MIN_ATR}×ATR)"
            return out
        retrace = (closes[-1] - leg_low) / leg
        if not (PULLBACK_MIN_RETRACE <= retrace <= PULLBACK_MAX_RETRACE):
            out["note"] = f"retrace {retrace:.0%} outside {PULLBACK_MIN_RETRACE:.0%}-{PULLBACK_MAX_RETRACE:.0%}"
            return out
        swing = min(float(b["l"]) for b in m15[-3:])
        out.update(ready=True, swing_level=round(swing, 2),
                   note=f"pullback {retrace:.0%} of {leg:.1f}pt leg — break {swing:.2f} to resume DOWN")
    return out


def analyze_mtf_confluence(m15_bars: list, live_price: float, atr15: float) -> dict:
    """Full cascade. `aligned` is True only when every layer confirms."""
    h4 = tf_trend(resample(m15_bars, 16))
    h1 = tf_trend(resample(m15_bars, 4))
    report = {
        "h4_trend": h4, "h1_structure": h1,
        "m15_setup": None, "entry_trigger": False,
        "aligned": False, "direction": None, "swing_level": None, "note": "",
    }
    if h4 == "FLAT" or h4 != h1:
        report["note"] = f"4H {h4} / 1H {h1} — no higher-TF agreement"
        return report
    direction = h4
    pb = detect_pullback(m15_bars, direction, atr15)
    report["m15_setup"] = pb
    if not pb["ready"]:
        report["note"] = f"4H+1H {direction} aligned, waiting on M15 setup: {pb['note']}"
        return report
    report["swing_level"] = pb["swing_level"]
    trigger = (live_price > pb["swing_level"] if direction == "UP"
               else live_price < pb["swing_level"])
    report["entry_trigger"] = bool(trigger)
    if trigger:
        report["aligned"] = True
        report["direction"] = "BUY" if direction == "UP" else "SELL"
        report["note"] = (f"CONFLUENCE: 4H {h4} + 1H {h1} + M15 {pb['note']} + "
                          f"live break of {pb['swing_level']} — {report['direction']}.")
    else:
        report["note"] = (f"4H+1H {direction} + M15 setup ready — waiting for live "
                          f"break of {pb['swing_level']} (now {live_price}).")
    return report


async def fetch_mtf_confluence(symbol: str, live_price: float) -> dict | None:
    """Cascade report from the freshest EA M15 stream (≤30 min old)."""
    try:
        from database import get_db
        from pip_utils import base_symbol
        from intraday_features import _atr
        doc = await get_db().intraday_candles.find_one(
            {"symbol": base_symbol(symbol)},
            {"bars": {"$slice": -800}, "updated_at": 1},
            sort=[("updated_at", -1)],
        )
        if not doc:
            return None
        upd = doc.get("updated_at")
        if upd:
            u = datetime.fromisoformat(str(upd).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - u > timedelta(minutes=30):
                return None
        bars = doc.get("bars") or []
        if len(bars) < 24:
            return None
        atr15 = _atr(bars[-30:], 14)
        return analyze_mtf_confluence(bars, live_price, atr15)
    except Exception as e:  # noqa: BLE001
        logger.debug("mtf confluence failed for %s: %s", symbol, e)
        return None
