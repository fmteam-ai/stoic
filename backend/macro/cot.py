"""CFTC Commitments-of-Traders (COT) — gold positioning extractor.

CFTC publishes the Disaggregated COT report every Friday for the prior
Tuesday. We pull the *gold* row (COMEX GC) and surface:

  - managed_money_long, managed_money_short   (raw contracts)
  - managed_money_net                          (longs - shorts)
  - managed_money_net_pct                      (net / open_interest * 100)
  - overcrowded_long, overcrowded_short        (booleans, percentile-based)

Source: Socrata JSON endpoint, public, no key required.
   https://publicreporting.cftc.gov/resource/72hh-3qpy.json
   filter: market_and_exchange_names="GOLD - COMMODITY EXCHANGE INC."

Cached in mongo (`cot_cache` collection) for 6h to avoid hammering the API.

`overcrowded_*` is computed against the 52-week percentile band stored alongside
the cache row (≥85th pct = overcrowded long, ≤15th pct = overcrowded short).
"""
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from database import get_db

logger = logging.getLogger("cot")

ENDPOINT = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
GOLD_MARKET = "GOLD - COMMODITY EXCHANGE INC."
CACHE_TTL_HOURS = 6
HTTP_TIMEOUT = float(os.environ.get("COT_HTTP_TIMEOUT", "10"))


async def _fetch_recent(weeks: int = 60) -> list[dict]:
    """Fetch up to `weeks` recent Tuesday rows for gold."""
    params = {
        "market_and_exchange_names": GOLD_MARKET,
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(weeks),
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(ENDPOINT, params=params)
        r.raise_for_status()
        return r.json()


def _safe_float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _summarise(row: dict) -> dict:
    long = _safe_float(row.get("m_money_positions_long_all"))
    short = _safe_float(row.get("m_money_positions_short_all"))
    oi = _safe_float(row.get("open_interest_all"))
    net = long - short
    net_pct = (net / oi * 100) if oi else 0.0
    return {
        "report_date": row.get("report_date_as_yyyy_mm_dd"),
        "open_interest": oi,
        "managed_money_long": long,
        "managed_money_short": short,
        "managed_money_net": net,
        "managed_money_net_pct": round(net_pct, 3),
    }


def _percentile_rank(value: float, series: list[float]) -> float:
    """Where does `value` sit in the historical distribution (0-100)?"""
    if not series:
        return 50.0
    sorted_s = sorted(series)
    below = sum(1 for v in sorted_s if v < value)
    return round(below / len(sorted_s) * 100, 2)


async def get_gold_positioning(*, force_refresh: bool = False) -> Optional[dict]:
    """Return the latest COT snapshot for gold, or None if unreachable."""
    db = get_db()
    if not force_refresh:
        cached = await db.cot_cache.find_one({"symbol": "XAUUSD"})
        if cached:
            fetched = cached.get("fetched_at")
            try:
                fetched_dt = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - fetched_dt < timedelta(hours=CACHE_TTL_HOURS):
                    cached.pop("_id", None)
                    return cached
            except (ValueError, TypeError):
                pass

    try:
        rows = await _fetch_recent(weeks=60)
    except httpx.HTTPError as e:
        logger.warning("CFTC COT fetch failed: %s", e)
        # Stale cache is better than nothing
        cached = await db.cot_cache.find_one({"symbol": "XAUUSD"})
        if cached:
            cached.pop("_id", None)
            return cached
        return None

    if not rows:
        return None

    summaries = [_summarise(r) for r in rows]
    latest = summaries[0]
    history_net_pct = [s["managed_money_net_pct"] for s in summaries[1:]]
    pct_rank = _percentile_rank(latest["managed_money_net_pct"], history_net_pct)
    latest["net_pct_percentile_52w"] = pct_rank
    latest["overcrowded_long"] = pct_rank >= 85
    latest["overcrowded_short"] = pct_rank <= 15
    latest["sample_size_weeks"] = len(history_net_pct)

    doc = {
        "symbol": "XAUUSD",
        **latest,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.cot_cache.update_one(
        {"symbol": "XAUUSD"}, {"$set": doc}, upsert=True
    )
    return doc
