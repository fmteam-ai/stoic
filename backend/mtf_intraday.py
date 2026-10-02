"""Multi-Timeframe Confluence engine (iter-125/127) — user-specified strategy.

Top-down cascade, all derived from the EA's M15 stream + live quote:
  4H  → overall trend        (resampled 16×M15)
  1H  → intermediate structure (resampled 4×M15)
  15M → setup (pullback toward EMA20 inside the higher-TF trend)
  entry → live-price breakout of the pullback's minor swing level
          (tick-precision via the live quote; the EA does not stream M5)

iter-127: three strictness modes so per-account presets map to real engines:
  strict   (Sniper)      — 4H must AGREE with 1H
  moderate (Balanced)    — 1H is the boss; 4H must not oppose
  relaxed  (Trend Rider) — 1H boss, wider pullback window, softer impulse
"""
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("mtf-confluence")

MTF_MODES = {
    "strict":   {"h4": "agree",        "retrace_lo": 0.25, "retrace_hi": 0.70, "impulse_atr": 2.0},
    "moderate": {"h4": "non_opposing", "retrace_lo": 0.20, "retrace_hi": 0.75, "impulse_atr": 1.75},
    "relaxed":  {"h4": "non_opposing", "retrace_lo": 0.15, "retrace_hi": 0.80, "impulse_atr": 1.5},
}


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


def detect_pullback(m15: list, direction: str, atr15: float,
                    retrace_lo: float = 0.25, retrace_hi: float = 0.70,
                    impulse_atr: float = 2.0) -> dict:
    """Impulse leg in `direction` + a retrace inside the window = setup.
    Returns the minor swing level whose break confirms trend resumption."""
    out = {"ready": False, "swing_level": None, "note": ""}
    if len(m15) < 20 or atr15 <= 0:
        out["note"] = "insufficient M15 data"
        return out
    closes = [float(b["c"]) for b in m15]
    seg = m15[-24:]
    leg_low = min(float(b["l"]) for b in seg)
    leg_high = max(float(b["h"]) for b in seg)
    leg = leg_high - leg_low
    if leg < impulse_atr * atr15:
        out["note"] = f"no impulse leg (range {leg:.1f} < {impulse_atr}×ATR)"
        return out
    if direction == "UP":
        retrace = (leg_high - closes[-1]) / leg
        if not (retrace_lo <= retrace <= retrace_hi):
            out["note"] = f"retrace {retrace:.0%} outside {retrace_lo:.0%}-{retrace_hi:.0%}"
            return out
        swing = max(float(b["h"]) for b in m15[-3:])
        out.update(ready=True, swing_level=round(swing, 2),
                   note=f"pullback {retrace:.0%} of {leg:.1f}pt leg — break {swing:.2f} to resume UP")
    else:
        retrace = (closes[-1] - leg_low) / leg
        if not (retrace_lo <= retrace <= retrace_hi):
            out["note"] = f"retrace {retrace:.0%} outside {retrace_lo:.0%}-{retrace_hi:.0%}"
            return out
        swing = min(float(b["l"]) for b in m15[-3:])
        out.update(ready=True, swing_level=round(swing, 2),
                   note=f"pullback {retrace:.0%} of {leg:.1f}pt leg — break {swing:.2f} to resume DOWN")
    return out


def analyze_mtf_confluence(m15_bars: list, live_price: float, atr15: float,
                           mode: str = "strict") -> dict:
    """Full cascade. `aligned` is True only when every layer confirms."""
    cfg = MTF_MODES.get(mode) or MTF_MODES["strict"]
    h4 = tf_trend(resample(m15_bars, 16))
    h1 = tf_trend(resample(m15_bars, 4))
    report = {
        "h4_trend": h4, "h1_structure": h1, "mode": mode,
        "m15_setup": None, "entry_trigger": False,
        "aligned": False, "direction": None, "swing_level": None, "note": "",
    }
    if cfg["h4"] == "agree":
        if h4 == "FLAT" or h4 != h1:
            report["note"] = f"4H {h4} / 1H {h1} — no higher-TF agreement"
            return report
        direction = h4
    else:
        # 1H is the boss; 4H just must not oppose it
        if h1 == "FLAT":
            report["note"] = f"1H FLAT (4H {h4}) — no intermediate trend to ride"
            return report
        if h4 not in (h1, "FLAT"):
            report["note"] = f"4H {h4} opposes 1H {h1} — standing aside"
            return report
        direction = h1
    pb = detect_pullback(m15_bars, direction, atr15,
                         retrace_lo=cfg["retrace_lo"],
                         retrace_hi=cfg["retrace_hi"],
                         impulse_atr=cfg["impulse_atr"])
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


async def fetch_mtf_confluence(symbol: str, live_price: float,
                               mode: str = "strict") -> dict | None:
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
        return analyze_mtf_confluence(bars, live_price, atr15, mode=mode)
    except Exception as e:  # noqa: BLE001
        logger.debug("mtf confluence failed for %s: %s", symbol, e)
        return None
