"""Economic calendar — Forex Factory weekly XML feed.

Free, no key required. Caches the parsed event list for 1 hour.

Macro-filter strategy (per Forex Factory convention):
  HIGH-impact events (Red Folder): Fed rate decisions, NFP, CPI, GDP, ECB, BOE…
  - 15 min before: bot enters MACRO_FREEZE — no new entries
  - 10 min after: still frozen — let institutional flow settle
  - Window can be tuned via MACRO_FREEZE_BEFORE_MIN / MACRO_FREEZE_AFTER_MIN env vars.
"""
import os
import time
import asyncio
import httpx
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

FF_FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

# Map symbol -> list of currencies it's sensitive to (Forex Factory country codes)
SYMBOL_CURRENCIES = {
    "XAUUSD":  {"USD", "ALL"},       # Gold is USD-driven + reacts to global risk
    "BTCUSD":  {"USD", "ALL"},       # Crypto reacts to Fed + global risk
    "ETHUSD":  {"USD", "ALL"},
    "SOLUSD":  {"USD", "ALL"},
    "BNBUSD":  {"USD", "ALL"},
    "XRPUSD":  {"USD", "ALL"},
    "ADAUSD":  {"USD", "ALL"},
    "DOGEUSD": {"USD", "ALL"},
    "EURUSD":  {"EUR", "USD"},
    "GBPUSD":  {"GBP", "USD"},
    "USDJPY":  {"USD", "JPY"},
    "AUDUSD":  {"AUD", "USD"},
    "USDCAD":  {"USD", "CAD"},
    "USDCHF":  {"USD", "CHF"},
    "NZDUSD":  {"NZD", "USD"},
}

UA = {"User-Agent": "Mozilla/5.0 (compatible; EmergentTradingBot/1.0)"}

# In-memory cache
_cache = {"events": None, "expires_at": 0}
_lock = asyncio.Lock()


def _before_min() -> int:
    return int(os.environ.get("MACRO_FREEZE_BEFORE_MIN", "15"))


def _after_min() -> int:
    return int(os.environ.get("MACRO_FREEZE_AFTER_MIN", "10"))


def _parse_event_time(date_str: str, time_str: str) -> datetime:
    """Parse Forex Factory format into UTC datetime.

    Date format: '06-22-2026', time format: '8:30am' / 'All Day' / 'Tentative'.
    All times in the feed are US Eastern Time (ET). We convert to UTC by assuming
    ET = UTC-4 (EDT, March-Nov) which covers most of the year. A naive but stable
    approximation — for high-impact events the bot will freeze within a generous
    window so a ~1h offset is acceptable.
    """
    try:
        dt = datetime.strptime(date_str.strip(), "%m-%d-%Y")
    except Exception:
        return None
    t = (time_str or "").strip().lower()
    if not t or t in ("all day", "tentative", "day 1", "day 2"):
        # No specific time → return midnight ET = 04:00 UTC for filtering purposes
        return dt.replace(hour=4, minute=0, tzinfo=timezone.utc)
    try:
        # e.g. "8:30am", "12:45pm"
        clean = t.replace(" ", "")
        h_part, ap = clean[:-2], clean[-2:]
        if ":" in h_part:
            h_str, m_str = h_part.split(":", 1)
        else:
            h_str, m_str = h_part, "0"
        h, m = int(h_str), int(m_str)
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        # Forex Factory feed is US Eastern; add 4h to convert to UTC (EDT approx).
        utc_dt = dt.replace(hour=h, minute=m, tzinfo=timezone.utc) + timedelta(hours=4)
        return utc_dt
    except Exception:
        return None


async def _fetch_events() -> list:
    async with httpx.AsyncClient(timeout=20.0, headers=UA, follow_redirects=True) as c:
        r = await c.get(FF_FEED)
        r.raise_for_status()
        xml_text = r.text
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    events = []
    for ev in root.findall("event"):
        title = (ev.findtext("title") or "").strip()
        country = (ev.findtext("country") or "").strip().upper()
        date_str = (ev.findtext("date") or "").strip()
        time_str = (ev.findtext("time") or "").strip()
        impact = (ev.findtext("impact") or "").strip().lower()
        forecast = (ev.findtext("forecast") or "").strip()
        previous = (ev.findtext("previous") or "").strip()
        when = _parse_event_time(date_str, time_str)
        if not when or not title:
            continue
        events.append({
            "title": title,
            "country": country,
            "impact": impact,            # "high" | "medium" | "low" | "holiday"
            "when": when.isoformat(),
            "when_ts": when.timestamp(),
            "forecast": forecast,
            "previous": previous,
        })
    return events


async def get_events() -> list:
    """Cached weekly events list."""
    if time.time() < _cache["expires_at"] and _cache["events"] is not None:
        return _cache["events"]
    async with _lock:
        if time.time() < _cache["expires_at"] and _cache["events"] is not None:
            return _cache["events"]
        try:
            evts = await _fetch_events()
            _cache["events"] = evts
            _cache["expires_at"] = time.time() + 3600  # 1h
            return evts
        except Exception:
            # Failure backoff (e.g. HTTP 429 rate limit): keep stale events and
            # stop hammering the feed for 10 min so the limit can reset.
            _cache["expires_at"] = time.time() + 600
            return _cache["events"] or []


def relevant_events_for(symbol: str, events: list) -> list:
    sym = symbol.upper()
    affecting = SYMBOL_CURRENCIES.get(sym, {"USD"})
    if "ALL" in affecting:
        return events
    return [e for e in events if e["country"] in affecting]


async def upcoming_for(symbol: str, hours: int = 24) -> list:
    """High/medium-impact events affecting `symbol` in the next `hours`."""
    events = await get_events()
    now = time.time()
    cutoff = now + hours * 3600
    filtered = []
    for e in relevant_events_for(symbol, events):
        if e["impact"] not in ("high", "medium"):
            continue
        ts = e["when_ts"]
        if now <= ts <= cutoff:
            filtered.append(e)
    return sorted(filtered, key=lambda x: x["when_ts"])


async def macro_freeze_check(symbol: str) -> dict:
    """Decide whether to freeze trading for `symbol` due to imminent macro event.

    Returns: { frozen: bool, reason: str, event: dict or None }
    """
    events = await get_events()
    now = time.time()
    before_s = _before_min() * 60
    after_s = _after_min() * 60
    for e in relevant_events_for(symbol, events):
        if e["impact"] != "high":
            continue
        ts = e["when_ts"]
        # Freeze window: [event - before_min, event + after_min]
        if (ts - before_s) <= now <= (ts + after_s):
            when_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            mins_to = int((ts - now) / 60)
            if mins_to >= 0:
                reason = (f"HIGH-impact {e['country']} event '{e['title']}' "
                          f"in {mins_to}min — bot frozen to avoid spread widening.")
            else:
                reason = (f"HIGH-impact {e['country']} event '{e['title']}' "
                          f"{abs(mins_to)}min ago — letting market settle.")
            return {
                "frozen": True,
                "reason": reason,
                "event": {**e, "when_human": when_dt.strftime("%Y-%m-%d %H:%M UTC")},
            }
    return {"frozen": False, "reason": "", "event": None}
