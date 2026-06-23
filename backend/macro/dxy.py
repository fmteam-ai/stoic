"""DXY (US Dollar Index) — inverse-correlation gate for XAUUSD.

Gold has a ~–0.85 correlation to the Dollar Index. This module pulls daily
DXY closes and computes a directional regime that the signal engine can use
to veto XAU trades that fight the dollar.

Data source: stooq.com daily CSV — free, keyless, stable.
  https://stooq.com/q/d/l/?s=^dxy&i=d&d1=YYYYMMDD&d2=YYYYMMDD

Regime classification:
  - "bullish_usd"  : last close > EMA20 AND 5-day slope > 0       → XAU shorts favoured
  - "bearish_usd"  : last close < EMA20 AND 5-day slope < 0       → XAU longs favoured
  - "neutral"      : mixed (price/slope disagree, or flat slope)   → no veto

Cached for 1h in `dxy_cache` collection.
"""
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from database import get_db

logger = logging.getLogger("dxy")

ENDPOINT_YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?range=6mo&interval=1d"
ENDPOINT_STOOQ = "https://stooq.com/q/d/l/?s=%5Edxy&i=d"
CACHE_TTL_HOURS = 1
HTTP_TIMEOUT = float(os.environ.get("DXY_HTTP_TIMEOUT", "10"))
EMA_PERIOD = int(os.environ.get("DXY_EMA_PERIOD", "20"))
SLOPE_LOOKBACK_DAYS = int(os.environ.get("DXY_SLOPE_LOOKBACK_DAYS", "5"))


def _ema(values: list[float], period: int) -> float:
    """Exponential moving average — returns the last EMA value."""
    if not values:
        return 0.0
    if len(values) < period:
        return sum(values) / len(values)
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period  # seed with SMA
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _slope(values: list[float]) -> float:
    """Simple slope: (last - first) / first, expressed as fractional change."""
    if len(values) < 2 or values[0] == 0:
        return 0.0
    return (values[-1] - values[0]) / values[0]


async def _fetch_yahoo() -> list[dict]:
    """Pull Yahoo Finance v8 chart API for DX-Y.NYB (DXY index).

    Returns [{date: 'YYYY-MM-DD', close: float}, ...] ascending by date.
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as c:
        r = await c.get(ENDPOINT_YAHOO, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return []
    res = result[0]
    timestamps = res.get("timestamp") or []
    closes_raw = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    rows: list[dict] = []
    for ts, close in zip(timestamps, closes_raw):
        if close is None:
            continue
        try:
            date_str = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
            rows.append({"date": date_str, "close": float(close)})
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda r: r["date"])
    return rows


async def _fetch_stooq() -> list[dict]:
    """Fallback: stooq daily CSV (may be JS-gated, returns [] if blocked)."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as c:
        r = await c.get(ENDPOINT_STOOQ, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        text = r.text
    if "<html" in text.lower():
        return []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or "Date" not in lines[0]:
        return []
    rows: list[dict] = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 5:
            continue
        try:
            close = float(parts[4])
            rows.append({"date": parts[0], "close": close})
        except (ValueError, IndexError):
            continue
    rows.sort(key=lambda r: r["date"])
    return rows


async def _fetch_csv() -> list[dict]:
    """Try Yahoo first, then stooq."""
    try:
        rows = await _fetch_yahoo()
        if rows:
            return rows
    except (httpx.HTTPError, ValueError, KeyError) as e:
        logger.warning("DXY Yahoo fetch failed: %s", e)
    try:
        return await _fetch_stooq()
    except httpx.HTTPError as e:
        logger.warning("DXY stooq fallback failed: %s", e)
        return []


def _classify(current: float, ema20: float, slope: float, slope_flat_threshold: float = 0.001) -> str:
    """Map (price vs EMA, slope sign) → regime label."""
    above_ema = current > ema20
    if abs(slope) < slope_flat_threshold:
        return "neutral"  # flat trend → don't block
    rising = slope > 0
    if above_ema and rising:
        return "bullish_usd"
    if not above_ema and not rising:
        return "bearish_usd"
    return "neutral"  # disagreement between price-vs-EMA and slope


async def get_dxy_snapshot(*, force_refresh: bool = False) -> Optional[dict]:
    """Return latest DXY metrics + regime label, or None on hard failure."""
    db = get_db()
    if not force_refresh:
        cached = await db.dxy_cache.find_one({"_id": "latest"})
        if cached:
            try:
                fetched = datetime.fromisoformat(str(cached.get("fetched_at")).replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - fetched < timedelta(hours=CACHE_TTL_HOURS):
                    cached.pop("_id", None)
                    return cached
            except (ValueError, TypeError):
                pass

    try:
        rows = await _fetch_csv()
    except httpx.HTTPError as e:
        logger.warning("DXY fetch failed: %s", e)
        cached = await db.dxy_cache.find_one({"_id": "latest"})
        if cached:
            cached.pop("_id", None)
            return cached
        return None

    if len(rows) < EMA_PERIOD + 1:
        return None

    closes = [r["close"] for r in rows]
    current_price = closes[-1]
    ema20 = _ema(closes[-(EMA_PERIOD * 3):], EMA_PERIOD)  # warm EMA on extra history
    slope = _slope(closes[-(SLOPE_LOOKBACK_DAYS + 1):])
    regime = _classify(current_price, ema20, slope)

    doc = {
        "current_price": round(current_price, 4),
        "ema_20": round(ema20, 4),
        "slope_5d_pct": round(slope * 100, 3),
        "above_ema": current_price > ema20,
        "regime": regime,
        "as_of_date": rows[-1]["date"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.dxy_cache.update_one({"_id": "latest"}, {"$set": doc}, upsert=True)
    return doc


def dxy_gate_check(action: str, symbol: str, dxy_snapshot: Optional[dict], strict: bool = False) -> dict:
    """Veto XAU trades that fight the dollar's prevailing direction.

    Only applies to XAUUSD. All other symbols pass through.

    Returns:
      { passed: bool, aligned: bool, reason: str, regime: str, dxy: dict|None }
    """
    if symbol.upper() != "XAUUSD":
        return {"passed": True, "aligned": True, "reason": "", "regime": "n/a", "dxy": None}
    if not dxy_snapshot:
        return {"passed": True, "aligned": True, "reason": "DXY data unavailable — gate skipped", "regime": "unknown", "dxy": None}
    if action not in ("BUY", "SELL"):
        return {"passed": True, "aligned": True, "reason": "", "regime": dxy_snapshot.get("regime", "neutral"), "dxy": dxy_snapshot}

    regime = dxy_snapshot.get("regime", "neutral")
    if regime == "neutral":
        return {"passed": True, "aligned": True, "reason": "DXY neutral — no inverse-correlation veto", "regime": regime, "dxy": dxy_snapshot}

    # XAU is inversely correlated to USD.
    # Bullish USD trend → XAU should be biased SHORT. BUY trades fight it → veto.
    # Bearish USD trend → XAU should be biased LONG.  SELL trades fight it → veto.
    if action == "BUY" and regime == "bullish_usd":
        return {
            "passed": False, "aligned": False,
            "reason": (
                f"DXY gate: dollar is bullish (DXY {dxy_snapshot['current_price']} > EMA20 "
                f"{dxy_snapshot['ema_20']}, 5d slope {dxy_snapshot['slope_5d_pct']}%). "
                f"XAU BUY fights inverse-correlation — vetoed."
            ),
            "regime": regime, "dxy": dxy_snapshot,
        }
    if action == "SELL" and regime == "bearish_usd":
        return {
            "passed": False, "aligned": False,
            "reason": (
                f"DXY gate: dollar is bearish (DXY {dxy_snapshot['current_price']} < EMA20 "
                f"{dxy_snapshot['ema_20']}, 5d slope {dxy_snapshot['slope_5d_pct']}%). "
                f"XAU SELL fights inverse-correlation — vetoed."
            ),
            "regime": regime, "dxy": dxy_snapshot,
        }

    # Action aligned with DXY (e.g. XAU SELL during bullish USD → reinforces, pass)
    aligned_msg = (
        f"DXY {regime} reinforces XAU {action} (inverse correlation aligned)"
        if not strict else
        f"DXY {regime} aligned with XAU {action}"
    )
    return {"passed": True, "aligned": True, "reason": aligned_msg, "regime": regime, "dxy": dxy_snapshot}
