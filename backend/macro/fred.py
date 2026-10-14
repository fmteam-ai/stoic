"""FRED (Federal Reserve Economic Data) — macro inputs for the Research Agent.

Five high-signal series:

  - DFF        : Federal Funds Effective Rate (overnight, daily)
  - DGS10      : 10-Year Treasury Constant Maturity Rate
  - UNRATE     : Civilian Unemployment Rate (monthly)
  - T10YIE     : 10-Year Breakeven Inflation Rate
  - VIXCLS     : CBOE Volatility Index

Transport (v1.60.31 item 1): the KEYED JSON API (api.stlouisfed.org, `FRED_API_KEY` from Admin → Integrations or
.env) whenever a key is set — the keyless `fredgraph.csv` endpoint times out from several VPS networks — and the
keyless CSV only as the no-key fallback. macro/tips.py uses the same transport for DFII10.

Returned snapshot includes the latest value, 30-day delta, and a regime label that Claude can incorporate into its
reasoning. Cached in `fred_cache` for 6h; an EMPTY fetch is never cached as a snapshot — it only arms a short
negative cache (FAIL_BACKOFF_MIN) and the previous snapshot is served as stale.
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
FAIL_BACKOFF_MIN = int(os.environ.get("FRED_FAIL_BACKOFF_MIN", "10"))
HTTP_TIMEOUT = float(os.environ.get("FRED_HTTP_TIMEOUT", "10"))
ENDPOINT = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
API_ENDPOINT = "https://api.stlouisfed.org/fred/series/observations"
LOOKBACK_DAYS = 400
_HEADERS = {"User-Agent": "STOIC/1.0"}


class FredError(RuntimeError):
    """Transport failure — message never carries the request URL (it would include the API key)."""


def api_key() -> str:
    return (os.environ.get("FRED_API_KEY") or "").strip()


def transport() -> str:
    return "fred_api" if api_key() else "fredgraph_csv"


def _parse_csv(text: str) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for line in StringIO(text):
        line = line.strip()
        if not line or line.lower().startswith("date"):
            continue
        parts = line.split(",")
        if len(parts) != 2 or parts[1] in (".", "", "NaN"):
            continue
        try:
            out.append((parts[0], float(parts[1])))
        except ValueError:
            continue
    return out


def _parse_api(payload: dict) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for o in payload.get("observations") or []:
        v = o.get("value")
        if v in (".", "", None, "NaN"):
            continue
        try:
            out.append((str(o.get("date")), float(v)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda r: r[0])
    return out


async def fetch_series(series_id: str, *, lookback_days: int = LOOKBACK_DAYS) -> list[tuple[str, float]]:
    """[(date, value), ...] oldest first — keyed API when FRED_API_KEY is set, keyless fredgraph.csv otherwise."""
    key = api_key()
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        if not key:
            r = await client.get(ENDPOINT.format(series=series_id), headers=_HEADERS)
            r.raise_for_status()
            return _parse_csv(r.text)
        start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()
        params = {"series_id": series_id, "api_key": key, "file_type": "json",
                  "sort_order": "asc", "observation_start": start}
        try:
            r = await client.get(API_ENDPOINT, params=params, headers=_HEADERS)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise FredError(f"HTTP {e.response.status_code} from the keyed FRED API for {series_id}") from None
        except httpx.HTTPError as e:
            raise FredError(f"{type(e).__name__} on the keyed FRED API for {series_id}") from None
        try:
            return _parse_api(r.json())
        except ValueError:
            raise FredError(f"non-JSON body from the keyed FRED API for {series_id}") from None


_fetch_series = fetch_series


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


def _fresh(doc: dict | None, hours: float) -> bool:
    """A snapshot is fresh only when it is recent AND carries at least one series — an EMPTY payload (pre-v1.60.31
    cache rows, or a bad write) must never satisfy the 6 h TTL and suppress a refetch."""
    if not ((doc or {}).get("payload") or {}).get("series"):
        return False
    try:
        at = datetime.fromisoformat(str((doc or {}).get("fetched_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - at < timedelta(hours=hours)


def _stale_payload(cached: dict | None, error: str | None) -> dict:
    base = dict((cached or {}).get("payload") or {"series": {}, "fetched_at": None})
    return {**base, "stale": True, "error": error}


async def get_macro_snapshot() -> dict:
    """Return the latest FRED snapshot (cached 6h).

    Shape:
      {
        "series": {"DFF": {"value":5.33, "date":"2026-02-22", "regime":"hold", "delta_30d": 0.0}, ...},
        "fetched_at": "2026-02-23T08:12:00Z",
        "stale": False,
        "transport": "fred_api" | "fredgraph_csv",
      }
    """
    db = get_db()
    cached = await db.fred_cache.find_one({"_id": "snapshot"})
    if cached and _fresh(cached, CACHE_TTL_HOURS):
        return {**cached.get("payload", {}), "stale": False}

    # negative cache — after an empty/failed fetch don't hammer FRED again for FAIL_BACKOFF_MIN
    neg = await db.fred_cache.find_one({"_id": "snapshot_fail"})
    if neg:
        try:
            until = datetime.fromisoformat(str(neg.get("retry_after")).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < until:
                return _stale_payload(cached, neg.get("last_error"))
        except ValueError:
            pass

    out: dict = {"series": {}, "transport": transport()}
    errors: list[str] = []
    for s in SERIES:
        try:
            rows = await fetch_series(s)
            if not rows:
                errors.append(f"{s}: no observations")
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
        except Exception as e:  # noqa: BLE001
            # never log str(e) of an httpx error: the request URL would carry the API key
            msg = str(e) if isinstance(e, FredError) else f"{type(e).__name__}"
            errors.append(f"{s}: {msg}")
            logger.warning("FRED fetch failed for %s: %s", s, msg)
            continue

    now = datetime.now(timezone.utc)
    if not out["series"]:
        err = "; ".join(errors)[:300] or "no series returned"
        await db.fred_cache.update_one(
            {"_id": "snapshot_fail"},
            {"$set": {"last_error": err, "failed_at": now.isoformat(),
                      "retry_after": (now + timedelta(minutes=FAIL_BACKOFF_MIN)).isoformat()}},
            upsert=True)
        logger.warning("FRED snapshot empty (%s) — serving previous snapshot, retry in %d min", err, FAIL_BACKOFF_MIN)
        return _stale_payload(cached, err)

    out["fetched_at"] = now.isoformat()
    if errors:
        out["partial"] = errors
    await db.fred_cache.update_one(
        {"_id": "snapshot"},
        {"$set": {"fetched_at": out["fetched_at"], "payload": out}},
        upsert=True,
    )
    await db.fred_cache.delete_many({"_id": "snapshot_fail"})
    return {**out, "stale": False}


__all__ = ["get_macro_snapshot", "fetch_series", "api_key", "transport", "FredError", "SERIES"]
