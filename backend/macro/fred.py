"""FRED (Federal Reserve Economic Data) — macro inputs for the Research Agent.

We pull five high-signal series via the keyless `fredgraph.csv` endpoint
(no API key required for public series, stable since 2010):

  - DFF        : Federal Funds Effective Rate (overnight, daily)
  - DGS10      : 10-Year Treasury Constant Maturity Rate
  - UNRATE     : Civilian Unemployment Rate (monthly)
  - T10YIE     : 10-Year Breakeven Inflation Rate
  - VIXCLS     : CBOE Volatility Index

Returned snapshot includes the latest value, 30-day delta, and a regime label
that Claude can incorporate into its reasoning.

Cached in `fred_cache` collection for 6h (these series update daily).
"""
import logging
import os
from datetime import datetime, timezone, timedelta
from io import StringIO

import httpx

from database import get_db

logger = logging.getLogger("fred")

SERIES = ["DFF", "DGS10", "UNRATE", "T10YIE", "VIXCLS"]
CACHE_TTL_HOURS = 6
HTTP_TIMEOUT = float(os.environ.get("FRED_HTTP_TIMEOUT", "10"))
ENDPOINT = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"


async def _fetch_series(series_id: str) -> list[tuple[str, float]]:
    """Fetch one series via fredgraph.csv (keyless). Returns [(date, value), ...]."""
    url = ENDPOINT.format(series=series_id)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        r = await client.get(url, headers={"User-Agent": "STOIC/1.0"})
        r.raise_for_status()
        out: list[tuple[str, float]] = []
        for line in StringIO(r.text):
            line = line.strip()
            if not line or line.lower().startswith("date"):
                continue
            parts = line.split(",")
            if len(parts) != 2:
                continue
            d, v = parts[0], parts[1]
            if v in (".", "", "NaN"):
                continue
            try:
                out.append((d, float(v)))
            except ValueError:
                continue
        return out


def _classify(series_id: str, latest: float, prev30: float | None) -> str:
    """Map (series, current, 30d-prior) to a one-word regime label."""
    if prev30 is None:
        return "neutral"
    delta = latest - prev30
    if series_id == "DFF":  # Fed funds rate
        if delta > 0.10:
            return "tightening"
        if delta < -0.10:
            return "easing"
        return "hold"
    if series_id == "DGS10":  # 10Y yield
        if delta > 0.25:
            return "yields_rising"
        if delta < -0.25:
            return "yields_falling"
        return "yields_stable"
    if series_id == "T10YIE":  # Inflation expectations
        if delta > 0.20:
            return "inflation_rising"
        if delta < -0.20:
            return "inflation_falling"
        return "inflation_stable"
    if series_id == "VIXCLS":  # Volatility
        if latest > 25:
            return "high_vol"
        if latest < 15:
            return "calm"
        return "normal_vol"
    if series_id == "UNRATE":
        if delta > 0.2:
            return "weakening_labor"
        if delta < -0.2:
            return "strengthening_labor"
        return "stable_labor"
    return "neutral"


async def get_macro_snapshot() -> dict:
    """Return the latest FRED snapshot (cached 6h).

    Shape:
      {
        "series": {"DFF": {"value":5.33, "date":"2026-02-22", "regime":"hold", "delta_30d": 0.0}, ...},
        "fetched_at": "2026-02-23T08:12:00Z",
        "stale": False,
      }
    """
    db = get_db()
    cached = await db.fred_cache.find_one({"_id": "snapshot"})
    if cached:
        try:
            fetched = datetime.fromisoformat(cached.get("fetched_at", "").replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - fetched < timedelta(hours=CACHE_TTL_HOURS):
                return {**cached.get("payload", {}), "stale": False}
        except Exception:
            pass

    out: dict = {"series": {}}
    for s in SERIES:
        try:
            rows = await _fetch_series(s)
            if not rows:
                continue
            latest_date, latest_val = rows[-1]
            # Find a value ~30 calendar days earlier (best-effort)
            cutoff = datetime.strptime(latest_date, "%Y-%m-%d") - timedelta(days=30)
            prev_val = None
            for d, v in reversed(rows[:-1]):
                if datetime.strptime(d, "%Y-%m-%d") <= cutoff:
                    prev_val = v
                    break
            out["series"][s] = {
                "value": round(latest_val, 3),
                "date": latest_date,
                "delta_30d": round(latest_val - prev_val, 3) if prev_val is not None else None,
                "regime": _classify(s, latest_val, prev_val),
            }
        except Exception as e:
            logger.warning("FRED fetch failed for %s: %s", s, e)
            continue

    out["fetched_at"] = datetime.now(timezone.utc).isoformat()
    await db.fred_cache.update_one(
        {"_id": "snapshot"},
        {"$set": {"fetched_at": out["fetched_at"], "payload": out}},
        upsert=True,
    )
    return {**out, "stale": False}


__all__ = ["get_macro_snapshot", "SERIES"]
