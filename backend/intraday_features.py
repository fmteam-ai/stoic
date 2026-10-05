"""Intraday M15 feature pack — gives the strategy engine real intraday vision.

The daily-anchored pipeline (MTF tiers, breakout scalper, VWAP pullback are
all computed from DAILY bars) is blind to intraday structure: on 2026-07-08
gold ranged 2.31% with a clean -75pt waterfall and the bot never saw it.
This module computes live features from the EA's M15 stream so (a) the LLM
sees today's price action and (b) the MTF veto can be overridden for
strongly-aligned intraday scalps.
"""
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("intraday-features")

MIN_BARS = 40
FRESHNESS_MIN = 30


def _ema(values: list, period: int) -> float:
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _atr(bars: list, period: int = 14) -> float:
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return 0.0
    return sum(trs[-period:]) / period


def compute_intraday_features(bars: list) -> dict | None:
    """Pure computation on M15 bars (dicts with t/o/h/l/c/v)."""
    if not bars or len(bars) < MIN_BARS:
        return None
    closes = [float(b["c"]) for b in bars]
    last = closes[-1]

    ema20 = _ema(closes[-40:], 20)
    ema50 = _ema(closes, 50) if len(closes) >= 50 else _ema(closes, 20)
    atr15 = _atr(bars[-30:], 14)

    # Trend state from EMA stack + slope of EMA20 over last 8 bars (2h)
    ema20_prev = _ema(closes[-48:-8], 20) if len(closes) >= 48 else ema20
    slope_pct = (ema20 - ema20_prev) / ema20_prev * 100 if ema20_prev else 0.0
    if last > ema20 > ema50 and slope_pct > 0.05:
        trend = "UP"
    elif last < ema20 < ema50 and slope_pct < -0.05:
        trend = "DOWN"
    else:
        trend = "FLAT"

    # 3-hour momentum (12 bars)
    mom_3h_pct = (last - closes[-13]) / closes[-13] * 100 if len(closes) >= 13 else 0.0

    # Donchian-20 breakout state
    hi20 = max(float(b["h"]) for b in bars[-21:-1])
    lo20 = min(float(b["l"]) for b in bars[-21:-1])
    if last > hi20:
        donchian = "BREAK_UP"
    elif last < lo20:
        donchian = "BREAK_DOWN"
    else:
        donchian = "INSIDE"

    # iter-128 · Breakout memory — any close outside its prior 20-bar
    # channel within the last 8 completed bars (~2h). Fade engines use it
    # to avoid catching falling knives right after a breakdown.
    recent_break = None
    if len(bars) >= 30:
        for k in range(max(21, len(bars) - 8), len(bars)):
            h = max(float(b["h"]) for b in bars[k - 20:k])
            lo = min(float(b["l"]) for b in bars[k - 20:k])
            c = float(bars[k]["c"])
            if c > h:
                recent_break = "UP"
            elif c < lo:
                recent_break = "DOWN"

    # Session VWAP — today's UTC bars, volume-weighted typical price
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pv = vol = 0.0
    for b in bars:
        if datetime.fromtimestamp(b["t"], tz=timezone.utc).strftime("%Y-%m-%d") != today:
            continue
        tp = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3
        v = float(b.get("v") or 1)
        pv += tp * v
        vol += v
    # Fix plan A3 — before the first bar of the UTC day there is NO session
    # VWAP: leave it None (callers must not read "None" as "price at VWAP").
    vwap = pv / vol if vol else None
    vwap_dist_pct = (last - vwap) / vwap * 100 if vwap else None

    # Swing structure — compare last two 10-bar segment extremes
    seg = bars[-30:]
    h1 = max(float(b["h"]) for b in seg[:10]); l1 = min(float(b["l"]) for b in seg[:10])
    h2 = max(float(b["h"]) for b in seg[10:20]); l2 = min(float(b["l"]) for b in seg[10:20])
    h3 = max(float(b["h"]) for b in seg[20:]); l3 = min(float(b["l"]) for b in seg[20:])
    if h3 > h2 > h1 and l3 > l2 > l1:
        structure = "HH_HL"       # bullish
    elif h3 < h2 < h1 and l3 < l2 < l1:
        structure = "LH_LL"       # bearish
    else:
        structure = "MIXED"

    day_bars = [b for b in bars
                if datetime.fromtimestamp(b["t"], tz=timezone.utc).strftime("%Y-%m-%d") == today]
    day_range_pct = None
    session_high = session_low = range_pos_pct = None
    if day_bars:
        dh = max(float(b["h"]) for b in day_bars)
        dl = min(float(b["l"]) for b in day_bars)
        day_range_pct = round((dh - dl) / dl * 100, 2) if dl else None
        session_high, session_low = round(dh, 2), round(dl, 2)
        if dh > dl:
            range_pos_pct = round((last - dl) / (dh - dl) * 100, 1)

    # iter-141 · typical (median) daily range of COMPLETE prior days — lets
    # gates scale with the current volatility regime instead of fixed %.
    typical_day_range_pct = None
    prior = {}
    for b in bars:
        d = datetime.fromtimestamp(b["t"], tz=timezone.utc).strftime("%Y-%m-%d")
        if d != today:
            e = prior.setdefault(d, [float(b["h"]), float(b["l"])])
            e[0] = max(e[0], float(b["h"]))
            e[1] = min(e[1], float(b["l"]))
    ranges = sorted((h - l) / l * 100 for h, l in prior.values() if l > 0)
    if len(ranges) >= 3:
        mid = len(ranges) // 2
        med = (ranges[mid] if len(ranges) % 2
               else (ranges[mid - 1] + ranges[mid]) / 2)
        typical_day_range_pct = round(med, 2)

    return {
        "timeframe": "M15",
        "last_price": round(last, 2),
        "trend": trend,
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "ema20_slope_pct_2h": round(slope_pct, 3),
        "momentum_3h_pct": round(mom_3h_pct, 3),
        "donchian20": donchian,
        "recent_break": recent_break,
        "session_vwap": round(vwap, 2) if vwap else None,
        "vwap_dist_pct": round(vwap_dist_pct, 3) if vwap_dist_pct is not None else None,
        "swing_structure": structure,
        "atr15": round(atr15, 3),
        "day_range_pct": day_range_pct,
        "typical_day_range_pct": typical_day_range_pct,
        "session_high": session_high,
        "session_low": session_low,
        "range_pos_pct": range_pos_pct,
        "bars_analyzed": len(bars),
    }


def intraday_alignment(action: str, feats: dict | None) -> tuple[int, str]:
    """Score 0-100 for how strongly M15 structure supports `action`."""
    if not feats or action not in ("BUY", "SELL"):
        return 0, ""
    up = action == "BUY"
    score = 0
    notes = []
    if feats["trend"] == ("UP" if up else "DOWN"):
        score += 35; notes.append(f"M15 trend {feats['trend']}")
    if feats["donchian20"] == ("BREAK_UP" if up else "BREAK_DOWN"):
        score += 20; notes.append(f"Donchian {feats['donchian20']}")
    mom = feats["momentum_3h_pct"]
    if (mom > 0.25 if up else mom < -0.25):
        score += 20; notes.append(f"3h momentum {mom:+.2f}%")
    if feats["swing_structure"] == ("HH_HL" if up else "LH_LL"):
        score += 15; notes.append(f"structure {feats['swing_structure']}")
    vd = feats.get("vwap_dist_pct")
    if vd is not None and (vd > 0 if up else vd < 0):
        score += 10; notes.append(f"price {'above' if up else 'below'} VWAP")
    return score, ", ".join(notes)


async def load_user_candles(symbol: str, user_id: str | None, timeframe: str = "M15",
                            last_n: int = 120, max_age_min: int = FRESHNESS_MIN) -> dict | None:
    """Fix plan A1 — the ONLY reader of intraday_candles: scoped to the user's own
    EA stream and the requested timeframe; never falls back to another user's bars.
    Returns the doc (bars UTC-normalised at ingest) or None when missing/stale."""
    if not user_id:
        return None
    from database import get_db
    from pip_utils import base_symbol
    doc = await get_db().intraday_candles.find_one(
        {"user_id": str(user_id), "symbol": base_symbol(symbol), "timeframe": timeframe},
        {"bars": {"$slice": -int(last_n)}, "updated_at": 1, "last_tick": 1, "t_basis": 1},
    )
    if not doc:
        return None
    upd = doc.get("updated_at")
    if upd:
        u = datetime.fromisoformat(str(upd).replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - u > timedelta(minutes=max_age_min):
            return None
    return doc


async def broker_live_price(symbol: str, user_id: str | None, max_age_s: int = 180) -> float | None:
    """Fix plan A10 — the user's broker tick (forming-bar close relayed by the EA),
    preferred over futures/daily public quotes when ≤ max_age_s old."""
    try:
        doc = await load_user_candles(symbol, user_id, last_n=1, max_age_min=max(1, max_age_s // 60 + 1))
        tick = (doc or {}).get("last_tick") or {}
        at = tick.get("at")
        if not at:
            return None
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(at).replace("Z", "+00:00"))).total_seconds()
        px = float(tick.get("price") or 0)
        return px if px > 0 and age <= max_age_s else None
    except Exception as e:  # noqa: BLE001
        logger.debug("broker tick unavailable for %s: %s", symbol, e)
        return None


async def fetch_intraday_pack(symbol: str, user_id: str | None = None, timeframe: str = "M15") -> dict | None:
    """Latest fresh M15 features for the symbol from the USER's EA candle stream."""
    try:
        doc = await load_user_candles(symbol, user_id, timeframe, last_n=120)
        if not doc:
            return None
        return compute_intraday_features(doc.get("bars") or [])
    except Exception as e:  # noqa: BLE001
        logger.debug("intraday pack failed for %s: %s", symbol, e)
        return None
