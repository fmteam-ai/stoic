"""US 10-Year TIPS Real Yield — fetched from the US Treasury's daily XML.

Why this matters for gold:
  Gold yields 0%. When real (inflation-adjusted) US interest rates drop,
  the opportunity cost of holding gold drops — bullish. Conversely, rising
  real yields are a headwind.

We use the *daily Treasury real yield curve* XML feed, which is public,
no auth needed:
  https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all?type=daily_treasury_real_yield_curve&field_tdr_date_value={year}&page&_format=csv

But the CSV path can be flaky; the XML feed is more stable:
  https://home.treasury.gov/sites/default/files/interest-rates/daily-treasury-real-yield-curve-rates-{year}.xml  -- not public

Pragmatic approach: use the XML "Atom feed" with year filter:
  https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_real_yield_curve&field_tdr_date_value={year}

We extract the most-recent two values and compute a delta. Surface:
  - real_yield_10y
  - real_yield_10y_prev
  - delta_5d   (rough — uses last 5 datapoints)
  - regime    ("bullish_gold" | "bearish_gold" | "neutral")
"""
import asyncio
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
import xml.etree.ElementTree as ET

import httpx

from database import get_db

logger = logging.getLogger("tips")

CACHE_TTL_HOURS = 12
FAIL_BACKOFF_MIN = int(os.environ.get("TIPS_FAIL_BACKOFF_MIN", "30"))
FAIL_BACKOFF_MAX_MIN = int(os.environ.get("TIPS_FAIL_BACKOFF_MAX_MIN", "360"))
HTTP_TIMEOUT = float(os.environ.get("TIPS_HTTP_TIMEOUT", "12"))


def _year_url(year: int) -> str:
    return (
        "https://home.treasury.gov/resource-center/data-chart-center/"
        "interest-rates/pages/xml?data=daily_treasury_real_yield_curve"
        f"&field_tdr_date_value={year}"
    )


# Atom namespaces in the Treasury feed
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
    "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
}


async def _fetch_year(year: int) -> list[dict]:
    """Return [{date, ten_year_real_yield}], oldest first."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(_year_url(year))
        r.raise_for_status()
        body = r.text
    return _parse_xml(body)


def _parse_xml(body: str) -> list[dict]:
    out: list[dict] = []
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        logger.warning("TIPS XML parse failed: %s", e)
        return out
    for entry in root.findall("a:entry", NS):
        props = entry.find("a:content/m:properties", NS)
        if props is None:
            continue
        date_el = props.find("d:NEW_DATE", NS)
        ten_el = props.find("d:TC_10YEAR", NS)
        if date_el is None or ten_el is None:
            continue
        try:
            ten = float(ten_el.text)
        except (TypeError, ValueError):
            continue
        out.append({
            "date": (date_el.text or "")[:10],
            "real_yield_10y": ten,
        })
    out.sort(key=lambda x: x["date"])
    return out


_FETCH_LOCK: tuple | None = None       # (loop, lock)


def _fetch_lock() -> asyncio.Lock:
    """One lock per event loop — created lazily so import never binds to a loop."""
    global _FETCH_LOCK
    loop = asyncio.get_event_loop()
    if _FETCH_LOCK is None or _FETCH_LOCK[0] is not loop:
        _FETCH_LOCK = (loop, asyncio.Lock())
    return _FETCH_LOCK[1]


async def get_real_yield(*, force_refresh: bool = False) -> Optional[dict]:
    """Latest 10Y TIPS real yield + delta + gold-regime classification.

    v1.60.32 B7 — single-flight: concurrent callers on a cold/expired cache wait for ONE fetch instead of each
    hitting Treasury/FRED and each logging "failure #1" at the same moment; the followers re-read the cache."""
    async with _fetch_lock():
        return await _get_real_yield_locked(force_refresh=force_refresh)


async def _get_real_yield_locked(*, force_refresh: bool = False) -> Optional[dict]:
    db = get_db()
    if not force_refresh:
        cached = await db.tips_cache.find_one({"key": "10Y_REAL"})
        if cached:
            try:
                fetched_dt = datetime.fromisoformat(
                    str(cached.get("fetched_at")).replace("Z", "+00:00")
                )
                if datetime.now(timezone.utc) - fetched_dt < timedelta(hours=CACHE_TTL_HOURS):
                    cached.pop("_id", None)
                    return cached
            except (ValueError, TypeError):
                pass

    year = datetime.now(timezone.utc).year
    # v1.60.29 item 9 — negative cache: after a failure, don't hit Treasury again for FAIL_BACKOFF_MIN (grows ×2 per
    # consecutive failure, capped). ~57 warnings/hour came from every tick retrying a host that times out from the VPS.
    neg = await db.tips_cache.find_one({"key": "10Y_REAL_FAIL"})
    if neg and not force_refresh:
        try:
            until = datetime.fromisoformat(str(neg.get("retry_after")).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < until:
                cached = await db.tips_cache.find_one({"key": "10Y_REAL"})
                if cached:
                    cached.pop("_id", None)
                return cached
        except (ValueError, TypeError):
            pass
    rows: list[dict] = []
    source = "treasury"
    try:
        rows = await _fetch_year(year)
        if len(rows) < 2:
            rows = (await _fetch_year(year - 1)) + rows
    except httpx.HTTPError as e:
        # secondary source: FRED DFII10 (10-year TIPS constant maturity) — keyed API when FRED_API_KEY is set
        try:
            from macro.fred import fetch_series, transport
            rows = [{"date": d, "real_yield_10y": v} for d, v in (await fetch_series("DFII10", lookback_days=120))[-60:]]
            source = f"fred:DFII10:{transport()}"
            logger.info("TIPS: Treasury unreachable (%s) — using FRED DFII10 (%d rows)", type(e).__name__, len(rows))
        except Exception as e2:  # noqa: BLE001
            fails = int((neg or {}).get("consecutive_failures") or 0) + 1
            backoff_min = min(FAIL_BACKOFF_MIN * (2 ** (fails - 1)), FAIL_BACKOFF_MAX_MIN)
            await db.tips_cache.update_one({"key": "10Y_REAL_FAIL"}, {"$set": {
                "key": "10Y_REAL_FAIL", "consecutive_failures": fails, "last_error": f"{type(e).__name__} / {type(e2).__name__}",
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "retry_after": (datetime.now(timezone.utc) + timedelta(minutes=backoff_min)).isoformat()}}, upsert=True)
            (logger.warning if fails <= 2 else logger.info)(
                "TIPS fetch failed (treasury: %s; fred: %s) — retry in %d min (failure #%d)", e, e2, backoff_min, fails)
            cached = await db.tips_cache.find_one({"key": "10Y_REAL"})
            if cached:
                cached.pop("_id", None)
                return cached
            return None
    await db.tips_cache.delete_many({"key": "10Y_REAL_FAIL"})

    if not rows:
        return None

    last = rows[-1]
    five_back = rows[-6] if len(rows) >= 6 else rows[0]
    delta_5d = last["real_yield_10y"] - five_back["real_yield_10y"]
    if delta_5d <= -0.05:        # real yield dropped ≥5bps over 5 days
        regime = "bullish_gold"
    elif delta_5d >= 0.05:
        regime = "bearish_gold"
    else:
        regime = "neutral"

    doc = {
        "key": "10Y_REAL",
        "real_yield_10y": round(last["real_yield_10y"], 4),
        "real_yield_10y_date": last["date"],
        "real_yield_10y_prev": round(rows[-2]["real_yield_10y"], 4) if len(rows) >= 2 else None,
        "delta_5d": round(delta_5d, 4),
        "regime": regime,
        "sample_size": len(rows),
        "source": source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.tips_cache.update_one({"key": "10Y_REAL"}, {"$set": doc}, upsert=True)
    return doc
