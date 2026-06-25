"""FRED macro feeds — daily series pulled hourly + cached in Mongo.

5 series tracked (driven by ai_signals risk context, not tick-frequency):
  DGS10    — 10-Year Treasury constant-maturity yield  (drives bond/gold dynamics)
  DGS2     — 2-Year Treasury constant-maturity yield   (Fed expectations proxy)
  FEDFUNDS — Effective Federal Funds Rate               (current Fed posture)
  DTWEXBGS — Trade-Weighted USD Index (Broad)           (DXY proxy for XAUUSD)
  T10YIE   — 10-Year Breakeven Inflation               (real-yield component)

FRED data updates ONCE per business day. We cache to `db.fred_cache` with a
1-hour TTL so the whole macro context costs ≤ 5 FRED calls/hour even if every
user hits the Dashboard.
"""
from datetime import datetime, timezone, timedelta
import asyncio
import logging
import os
import httpx

from database import get_db

logger = logging.getLogger("macro-feeds")

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
CACHE_TTL_SEC = 3600  # 1 hour

SERIES = {
    "DGS10":    {"name": "10Y Treasury Yield",    "unit": "%", "decimals": 2},
    "DGS2":     {"name": "2Y Treasury Yield",     "unit": "%", "decimals": 2},
    "FEDFUNDS": {"name": "Fed Funds Rate",        "unit": "%", "decimals": 2},
    "DTWEXBGS": {"name": "USD Index (Broad)",     "unit": "",  "decimals": 2},
    "T10YIE":   {"name": "10Y Inflation Breakeven","unit": "%", "decimals": 2},
}

_fetch_locks: dict = {}


def _lock_for(series_id: str) -> asyncio.Lock:
    if series_id not in _fetch_locks:
        _fetch_locks[series_id] = asyncio.Lock()
    return _fetch_locks[series_id]


def _api_key() -> str:
    return os.environ.get("FRED_API_KEY", "")


async def _fetch_series(series_id: str) -> dict | None:
    """Pull last ~15 observations for a series and compute deltas.

    Returns None on missing API key, network failure, or insufficient data —
    the caller (route) will fall back to whatever's already cached in Mongo.
    """
    key = _api_key()
    if not key:
        logger.warning("FRED_API_KEY not set — skipping fetch for %s", series_id)
        return None
    params = {
        "series_id": series_id,
        "api_key": key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 15,  # enough to skip weekends/holidays + cover 1w lookback
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(FRED_BASE, params=params)
            if r.status_code != 200:
                logger.error("FRED %s returned %d", series_id, r.status_code)
                return None
            data = r.json()
    except Exception as e:  # noqa: BLE001
        logger.error("FRED fetch %s failed: %s", series_id, e)
        return None

    # FRED returns "." for bank-holiday gaps — strip them
    valid = [o for o in data.get("observations", []) if o.get("value") not in (".", None, "")]
    if len(valid) < 6:
        logger.warning("FRED %s — only %d valid observations", series_id, len(valid))
        return None

    try:
        latest_val = float(valid[0]["value"])
        dod_val = float(valid[1]["value"])
        wow_val = float(valid[5]["value"])  # 5 trading days ago = ~1 week
    except (KeyError, ValueError) as e:
        logger.error("FRED %s parse error: %s", series_id, e)
        return None

    meta = SERIES[series_id]
    dp = meta["decimals"]
    return {
        "series_id": series_id,
        "name": meta["name"],
        "unit": meta["unit"],
        "latest": round(latest_val, dp),
        "date": valid[0]["date"],
        "dod_delta": round(latest_val - dod_val, dp + 1),
        "wow_delta": round(latest_val - wow_val, dp + 1),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


async def get_macro_snapshot(force_refresh: bool = False) -> dict:
    """Return the full macro snapshot. Per-series cached for CACHE_TTL_SEC.

    Shape:
      {
        "series": [ {series_id, name, latest, date, dod_delta, wow_delta, unit, stale_cache?}, ... ],
        "checked_at": iso,
        "has_api_key": bool,
        "stale_cache": bool,   # any series fell back to stale cache
      }
    """
    from database import get_db as _get_db
    db = _get_db() if "get_db" not in globals() else get_db()
    now = datetime.now(timezone.utc)
    has_key = bool(_api_key())

    out_series: list[dict] = []
    stale_cache_flag = False

    for series_id in SERIES.keys():
        # Single-flight per series — prevents stampedes if 50 Dashboards load at once
        async with _lock_for(series_id):
            cached = await db.fred_cache.find_one({"_id": series_id})
            cached_age = None
            if cached:
                try:
                    fetched_at = datetime.fromisoformat(str(cached.get("fetched_at")).replace("Z", "+00:00"))
                    cached_age = (now - fetched_at).total_seconds()
                except Exception:
                    cached_age = None

            # Cache hit: under TTL → serve immediately
            if not force_refresh and cached_age is not None and cached_age <= CACHE_TTL_SEC:
                payload = {k: v for k, v in cached.items() if k != "_id"}
                out_series.append(payload)
                continue

            # Cache miss / expired → fetch fresh
            fresh = await _fetch_series(series_id)
            if fresh:
                await db.fred_cache.update_one(
                    {"_id": series_id}, {"$set": fresh}, upsert=True,
                )
                out_series.append(fresh)
            elif cached:
                # API failed → serve stale cache + flag it
                payload = {k: v for k, v in cached.items() if k != "_id"}
                payload["stale_cache"] = True
                out_series.append(payload)
                stale_cache_flag = True

    return {
        "series": out_series,
        "checked_at": now.isoformat(),
        "has_api_key": has_key,
        "stale_cache": stale_cache_flag,
    }
