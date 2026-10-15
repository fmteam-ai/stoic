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
    "XAUUSD":  {"USD", "EUR", "GBP", "JPY", "CNY"},   # v1.60.39 C8 — explicit, per-account (gold_freeze_currencies)
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

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

# In-memory cache
_cache = {"events": None, "expires_at": 0, "fetched_at": 0, "last_error": None}
_lock = asyncio.Lock()


def _before_min() -> int:
    return int(os.environ.get("MACRO_FREEZE_BEFORE_MIN", "15"))


def _after_min() -> int:
    return int(os.environ.get("MACRO_FREEZE_AFTER_MIN", "10"))


# v1.60.39 (C8) — which currencies' HIGH-impact events freeze gold. Was implicitly every country ("ALL"):
# 15 Oct UK GDP froze gold 05:45–06:10. Default = the five that actually move XAUUSD.
GOLD_FREEZE_CURRENCIES_DEFAULT = ["USD", "EUR", "GBP", "JPY", "CNY"]
MEDIUM_FREEZE_MIN_DEFAULT = 5          # C9 — USD medium-impact: ±5 min
AVALANCHE_AFTER_MIN_DEFAULT = 20       # C10 — ≥2 USD high-impact prints within 5 min → after-window 20 min
AVALANCHE_CLUSTER_MIN = 5


def _cfg_int(cfg: dict | None, key: str, default: int, lo: int = 0, hi: int = 240) -> int:
    try:
        v = int((cfg or {}).get(key) if (cfg or {}).get(key) is not None else default)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def freeze_settings(cfg: dict | None = None) -> dict:
    """Per-account freeze windows (Bot Config), env/legacy defaults when unset."""
    cfg = cfg or {}
    cur = cfg.get("gold_freeze_currencies")
    if not isinstance(cur, (list, tuple, set)) or not cur:
        cur = GOLD_FREEZE_CURRENCIES_DEFAULT
    return {
        "before_min": _cfg_int(cfg, "macro_freeze_before_min", _before_min()),
        "after_min": _cfg_int(cfg, "macro_freeze_after_min", _after_min()),
        "medium_usd_enabled": bool(cfg.get("macro_freeze_medium_usd_enabled", True)),
        "medium_min": _cfg_int(cfg, "macro_freeze_medium_min", MEDIUM_FREEZE_MIN_DEFAULT, 0, 60),
        "avalanche_after_min": _cfg_int(cfg, "macro_freeze_avalanche_after_min", AVALANCHE_AFTER_MIN_DEFAULT),
        "gold_freeze_currencies": sorted({str(c).upper() for c in cur}),
    }


def freeze_windows(symbol: str, events: list, cfg: dict | None = None) -> list:
    """Every freeze window for `symbol`, sorted by start: [{start_ts, end_ts, kind, event, ...}].

    kind: "high" (before/after around a HIGH-impact print), "avalanche" (≥2 USD HIGH prints within
    AVALANCHE_CLUSTER_MIN → longer after-window), "medium" (USD medium-impact ±medium_min, only when the
    print is not already inside a high window)."""
    st = freeze_settings(cfg)
    before_s, after_s = st["before_min"] * 60, st["after_min"] * 60
    relevant = relevant_events_for(symbol, events, cfg)
    highs = [e for e in relevant if e.get("impact") == "high"]
    usd_high_ts = sorted(e["when_ts"] for e in highs if e.get("country") == "USD")
    windows = []
    for e in highs:
        ts = e["when_ts"]
        cluster = [t for t in usd_high_ts if abs(t - ts) <= AVALANCHE_CLUSTER_MIN * 60]
        avalanche = e.get("country") == "USD" and len(cluster) >= 2
        a = st["avalanche_after_min"] * 60 if avalanche else after_s
        windows.append({"start_ts": ts - before_s, "end_ts": ts + a, "kind": "avalanche" if avalanche else "high",
                        "event": e, "cluster_size": len(cluster) if avalanche else 1})
    if st["medium_usd_enabled"] and st["medium_min"] > 0 and "USD" in _affecting(symbol, cfg):
        m_s = st["medium_min"] * 60
        for e in events:
            if e.get("impact") != "medium" or e.get("country") != "USD":
                continue
            ts = e["when_ts"]
            if any(w["start_ts"] <= ts <= w["end_ts"] for w in windows):
                continue
            windows.append({"start_ts": ts - m_s, "end_ts": ts + m_s, "kind": "medium", "event": e, "cluster_size": 1})
    windows.sort(key=lambda w: w["start_ts"])
    return windows


def _window_reason(w: dict, now: float) -> str:
    e = w["event"]
    ts = e["when_ts"]
    mins_to = int((ts - now) / 60)
    lvl = {"high": "HIGH-impact", "avalanche": f"MACRO AVALANCHE ({w.get('cluster_size')} USD HIGH-impact prints)",
           "medium": "USD medium-impact"}[w["kind"]]
    if mins_to >= 0:
        return f"{lvl} {e['country']} event '{e['title']}' in {mins_to}min — bot frozen to avoid spread widening."
    settle = "letting market settle" if w["kind"] != "medium" else "short settle window"
    return f"{lvl} {e['country']} event '{e['title']}' {abs(mins_to)}min ago — {settle}."


def _describe_window(w: dict) -> dict:
    e = w["event"]
    return {**e, "when_human": datetime.fromtimestamp(e["when_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "freeze_kind": w["kind"], "freeze_start": datetime.fromtimestamp(w["start_ts"], tz=timezone.utc).isoformat(),
            "freeze_end": datetime.fromtimestamp(w["end_ts"], tz=timezone.utc).isoformat(),
            "cluster_size": w.get("cluster_size", 1)}


def next_freeze(symbol: str, events: list, cfg: dict | None = None, now: float | None = None) -> dict | None:
    """The next freeze window that has not started yet — "next freeze: <event> at <time>" on the dashboard."""
    now = time.time() if now is None else now
    for w in freeze_windows(symbol, events, cfg):
        if w["start_ts"] > now:
            return _describe_window(w)
    return None


def _parse_event_time(date_str: str, time_str: str) -> datetime:
    """Parse Forex Factory format into UTC datetime.

    Date format: '06-22-2026', time format: '12:30pm' / 'All Day' / 'Tentative'.
    iter-131 · The feed's times are ALREADY UTC (verified 2026-07-14: feed said
    12:30pm for US CPI and the actual spike hit 12:30 UTC = 8:30am ET). The old
    +4h "ET→UTC" shift double-converted, so every pre-event guard armed 4 hours
    late and the bot had zero calendar protection during the CPI print.
    """
    try:
        dt = datetime.strptime(date_str.strip(), "%m-%d-%Y")
    except Exception:
        return None
    t = (time_str or "").strip().lower()
    if not t or t in ("all day", "tentative", "day 1", "day 2"):
        # No specific time → midnight UTC for filtering purposes
        return dt.replace(hour=0, minute=0, tzinfo=timezone.utc)
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
        return dt.replace(hour=h, minute=m, tzinfo=timezone.utc)
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
            _cache["fetched_at"] = time.time()
            _cache["last_error"] = None
            await _persist_status()
            return evts
        except Exception as e:
            # Failure backoff (e.g. HTTP 429 rate limit): keep stale events and
            # stop hammering the feed for 10 min so the limit can reset.
            _cache["expires_at"] = time.time() + 600
            _cache["last_error"] = f"{type(e).__name__}: {e}"
            await _persist_status()
            return _cache["events"] or []


async def _persist_status() -> None:
    """v1.60.39 — Bot Health (API process) reads the calendar status the worker recorded."""
    try:
        from news_status import record_source_status
        await record_source_status("calendar", feed_status())
    except Exception:  # noqa: BLE001
        pass


def feed_status() -> dict:
    """Diagnostics for UIs: provider health of the calendar feed."""
    now = time.time()
    fetched = _cache.get("fetched_at") or 0
    return {
        "provider": "ForexFactory (weekly XML)",
        "last_fetch_at": (datetime.fromtimestamp(fetched, tz=timezone.utc).isoformat()
                          if fetched else None),
        "last_fetch_age_min": round((now - fetched) / 60.0, 1) if fetched else None,
        "cached_events": len(_cache.get("events") or []),
        "last_error": _cache.get("last_error"),
        "status": ("OK" if _cache.get("events") and not _cache.get("last_error")
                   else "DEGRADED" if _cache.get("events")
                   else "DOWN"),
        "timezone": "UTC (feed times parsed verbatim, iter-131)",
    }


def _affecting(symbol: str, cfg: dict | None = None) -> set:
    sym = symbol.upper()
    if sym == "XAUUSD":
        return set(freeze_settings(cfg)["gold_freeze_currencies"])
    return set(SYMBOL_CURRENCIES.get(sym, {"USD"}))


def relevant_events_for(symbol: str, events: list, cfg: dict | None = None) -> list:
    """Events whose country can freeze `symbol`. Gold is explicit + per-account (C8); crypto keeps "ALL"."""
    affecting = _affecting(symbol, cfg)
    if "ALL" in affecting:
        return events
    return [e for e in events if e["country"] in affecting]


async def upcoming_for(symbol: str, hours: int = 24, cfg: dict | None = None) -> list:
    """High/medium-impact events affecting `symbol` in the next `hours`."""
    events = await get_events()
    now = time.time()
    cutoff = now + hours * 3600
    filtered = []
    for e in relevant_events_for(symbol, events, cfg):
        if e["impact"] not in ("high", "medium"):
            continue
        ts = e["when_ts"]
        if now <= ts <= cutoff:
            filtered.append(e)
    return sorted(filtered, key=lambda x: x["when_ts"])


async def macro_freeze_check(symbol: str, cfg: dict | None = None) -> dict:
    """Decide whether to freeze trading for `symbol` due to an imminent/just-passed macro event.

    Returns: { frozen: bool, reason: str, event: dict or None, kind, next_freeze }
    """
    events = await get_events()
    now = time.time()
    windows = freeze_windows(symbol, events, cfg)
    for w in windows:
        if w["start_ts"] <= now <= w["end_ts"]:
            return {"frozen": True, "reason": _window_reason(w, now), "event": _describe_window(w),
                    "kind": w["kind"], "next_freeze": next_freeze(symbol, events, cfg, now)}
    return {"frozen": False, "reason": "", "event": None, "kind": None,
            "next_freeze": next_freeze(symbol, events, cfg, now)}
