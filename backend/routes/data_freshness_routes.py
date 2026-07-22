"""Data-freshness API — surfaces last-fetch timestamps for every external
data source the bot depends on. Powers the dashboard's "data health" strip
so users can immediately see if anything is stale.

  GET /api/data-freshness
  → {
      "fred":      {"last_fetched_at": ..., "age_seconds": ...,  "stale": false},
      "news":      {"last_fetched_at": ..., "age_seconds": ...,  "stale": false},
      "calendar":  {"last_fetched_at": ..., "age_seconds": ...,  "stale": false},
      "ea":        {"last_heartbeat_at": ..., "age_seconds": ...,"stale": false, "account_count": 1},
      "agents":    {"last_activity_at": ..., "age_seconds": ...,"stale": false},
      "evaluated_at": "..."
    }

A source is `stale` when `age_seconds > threshold`. Thresholds are tuned per
source — FRED is updated daily so 6h is fine; EA heartbeat is 5s so >60s is bad.
"""
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db
from macro_feeds import get_macro_snapshot

router = APIRouter(prefix="/data-freshness", tags=["health"])

# Per-source staleness thresholds (seconds)
THRESHOLDS = {
    "fred":     6 * 3600,      # FRED data updates ~daily
    "news":     30 * 60,       # news is freshness-sensitive
    "calendar": 6 * 3600,      # event calendar polled hourly
    "ea":       60,            # heartbeats every 5s; >60s = disconnected
    "candles":  20 * 60,       # M15 stream pushes every ~5min; >20min = stale
    "agents":   24 * 3600,     # only stale if bot is genuinely idle
}


def _age(iso_or_dt) -> tuple[float, str | None]:
    """Returns (seconds_since, normalised_iso_string)."""
    if not iso_or_dt:
        return float("inf"), None
    try:
        if isinstance(iso_or_dt, datetime):
            dt = iso_or_dt if iso_or_dt.tzinfo else iso_or_dt.replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(iso_or_dt).replace("Z", "+00:00"))
    except Exception:
        return float("inf"), None
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return max(0.0, age), dt.isoformat()


def _entry(threshold_key: str, ts):
    age, iso = _age(ts)
    return {
        "last_fetched_at": iso,
        "age_seconds": None if age == float("inf") else round(age),
        "stale": age > THRESHOLDS[threshold_key],
        "threshold_seconds": THRESHOLDS[threshold_key],
    }


@router.get("")
async def data_freshness(user=Depends(get_current_user)):
    db = get_db()
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. FRED — snapshot has a per-series fetched_at; take the most recent.
    try:
        snap = await get_macro_snapshot()
        series = snap.get("series") if isinstance(snap, dict) else []
        fred_ts = max(
            (s.get("fetched_at") for s in (series or []) if s.get("fetched_at")),
            default=None,
        )
    except Exception:
        fred_ts = None

    # 2. News — last news_articles doc
    try:
        news_doc = await db.news_articles.find().sort("fetched_at", -1).limit(1).to_list(length=1)
        news_ts = news_doc[0]["fetched_at"] if news_doc else None
    except Exception:
        news_ts = None

    # 3. Calendar — last economic_events doc
    try:
        cal_doc = await db.economic_events.find().sort("fetched_at", -1).limit(1).to_list(length=1)
        cal_ts = cal_doc[0]["fetched_at"] if cal_doc else None
    except Exception:
        cal_ts = None

    # 4. EA heartbeat — most recent across user's accounts
    accts = await db.accounts.find(
        {"user_id": user["id"]}, {"last_heartbeat": 1, "status": 1},
    ).to_list(length=20)
    hb_ts = max((a.get("last_heartbeat") for a in accts if a.get("last_heartbeat")), default=None)
    ea_entry = _entry("ea", hb_ts)
    ea_entry["account_count"] = len(accts)
    ea_entry["connected_count"] = sum(1 for a in accts if a.get("status") == "connected")
    # Rename for clarity — heartbeats aren't "fetches"
    ea_entry["last_heartbeat_at"] = ea_entry.pop("last_fetched_at")

    # 5. Agent activity — last tick for this user
    try:
        agent_doc = await db.agent_activity.find(
            {"user_id": user["id"]},
        ).sort("started_at", -1).limit(1).to_list(length=1)
        agent_ts = agent_doc[0].get("started_at") if agent_doc else None
    except Exception:
        agent_ts = None

    # 6. P0-4 · Candle feed — per-symbol pipeline health (bridge /candles)
    candles = {}
    try:
        async for c in db.candle_feed_health.find({"user_id": user["id"]}):
            age, iso = _age(c.get("last_received_at"))
            candles[f"{c.get('symbol')}_{c.get('timeframe')}"] = {
                "symbol": c.get("symbol"),
                "timeframe": c.get("timeframe"),
                "source_symbol": c.get("source_symbol"),
                "last_received_at": iso,
                "age_seconds": None if age == float("inf") else round(age),
                "bar_lag_s": c.get("bar_lag_s"),
                "valid_bars": c.get("valid_bars"),
                "dropped_bars": c.get("dropped_bars"),
                "payloads_received": c.get("payloads_received"),
                "payloads_rejected": c.get("payloads_rejected", 0),
                "last_write_ok": c.get("last_write_ok"),
                "last_error": c.get("last_error"),
                "stale": age > THRESHOLDS["candles"],
            }
    except Exception:
        pass

    return {
        "fred":     _entry("fred",     fred_ts),
        "news":     _entry("news",     news_ts),
        "calendar": _entry("calendar", cal_ts),
        "ea":       ea_entry,
        "agents":   _entry("agents",   agent_ts),
        "candles":  candles,
        "evaluated_at": now_iso,
    }
