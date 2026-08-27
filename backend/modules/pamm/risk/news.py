"""PAMM news filter — free ForexFactory weekly calendar feed (no API key).
High-impact events open a blackout window that vetoes master-account trading.
Feed is cached in Mongo (pamm_news_cache) and refreshed hourly."""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("pamm.news")

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_TTL_S = 3600
_IMPACT_RANK = {"low": 1, "medium": 2, "high": 3, "holiday": 3}


def _norm(e: dict) -> dict:
    return {"title": str(e.get("title") or "")[:120],
            "country": str(e.get("country") or "")[:8],
            "impact": str(e.get("impact") or "").lower(),
            "date": e.get("date"),
            "forecast": e.get("forecast"), "previous": e.get("previous")}


async def get_calendar(db, force: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    cache = await db.pamm_news_cache.find_one({"_id": "ff_thisweek"})
    if cache and not force:
        age = (now - datetime.fromisoformat(cache["fetched_at"])
               ).total_seconds()
        if age < CACHE_TTL_S:
            return {"events": cache["events"],
                    "fetched_at": cache["fetched_at"], "source": "cache"}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(FEED_URL)
            r.raise_for_status()
            raw = r.json()
        events = [_norm(e) for e in raw if e.get("date")]
        await db.pamm_news_cache.replace_one(
            {"_id": "ff_thisweek"},
            {"_id": "ff_thisweek", "fetched_at": now.isoformat(),
             "events": events}, upsert=True)
        return {"events": events, "fetched_at": now.isoformat(),
                "source": "feed"}
    except Exception as e:
        logger.warning("news feed fetch failed: %s", e)
        if cache:  # stale cache beats nothing
            return {"events": cache["events"],
                    "fetched_at": cache["fetched_at"], "source": "stale",
                    "error": str(e)[:200]}
        return {"events": [], "source": "unavailable",
                "error": str(e)[:200]}


def _event_time(e: dict) -> datetime | None:
    try:
        dt = datetime.fromisoformat(e["date"])
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(
            tzinfo=timezone.utc)
    except Exception:
        return None


async def news_blackout_status(db, cfg: dict,
                               now: datetime | None = None) -> dict:
    """Is a blackout window active right now? cfg = news_filter limits."""
    if not cfg.get("enabled"):
        return {"enabled": False, "active": False}
    now = now or datetime.now(timezone.utc)
    cal = await get_calendar(db)
    min_rank = _IMPACT_RANK.get(str(cfg.get("min_impact", "high")).lower(), 3)
    before = timedelta(minutes=int(cfg.get("blackout_before_min", 30)))
    after = timedelta(minutes=int(cfg.get("blackout_after_min", 15)))
    active, upcoming = None, []
    for e in cal["events"]:
        if _IMPACT_RANK.get(e["impact"], 0) < min_rank:
            continue
        et = _event_time(e)
        if not et:
            continue
        if et - before <= now <= et + after:
            active = {**e, "window_ends": (et + after).isoformat()}
            break
        if now < et - before and et - now <= timedelta(hours=24):
            upcoming.append({**e, "starts_in_min":
                             round((et - before - now).total_seconds() / 60)})
    upcoming.sort(key=lambda x: x["starts_in_min"])
    return {"enabled": True, "active": bool(active), "event": active,
            "upcoming": upcoming[:5], "feed_source": cal["source"],
            "feed_error": cal.get("error")}
