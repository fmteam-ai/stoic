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


# ---------------------------------------------------------------------------
# v1.60.40 — second source + merge + staleness + fail-safe
# ---------------------------------------------------------------------------
FC_FEED = "https://www.financecalendar.com/wp-json/fc/v1/calendar"
FC_ATTRIBUTION = {"source": "financecalendar.com", "url": "https://www.financecalendar.com",
                  "terms": "Free for any use with attribution (link to financecalendar.com)"}
SOURCE_ORDER = ("forexfactory", "financecalendar")
STALE_AFTER_SEC = 2 * 3600          # Bot Health alert: active source older than this
FAILSAFE_AFTER_SEC = 6 * 3600       # both sources older than this → conservative default freeze
_sources: dict = {name: {"events": None, "fetched_at": 0, "last_error": None} for name in SOURCE_ORDER}
_last_merge: dict = {"at": 0, "events": None, "failsafe": False}

# Fail-safe schedule (UTC). 12:30 = CPI/PPI/NFP/Retail Sales/GDP/claims; 14:00 = ISM/Michigan/home sales;
# 18:00 on FOMC statement days. FOMC dates per the Fed's published calendars (second meeting day).
FAILSAFE_TIMES_UTC = ((12, 30), (14, 0))
FOMC_STATEMENT_DAYS = {
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-16", "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
}

_FC_COUNTRY_PREFIX = (("US ", "USD"), ("UK ", "GBP"), ("ECB", "EUR"), ("Eurozone", "EUR"), ("Euro ", "EUR"),
                      ("Germany", "EUR"), ("France", "EUR"), ("Italy", "EUR"), ("Spain", "EUR"), ("FOMC", "USD"),
                      ("Fed ", "USD"), ("Japan", "JPY"), ("BoJ", "JPY"), ("China", "CNY"), ("PBoC", "CNY"),
                      ("Bank of Canada", "CAD"), ("Canada", "CAD"), ("Australia", "AUD"), ("RBA", "AUD"),
                      ("RBNZ", "NZD"), ("New Zealand", "NZD"), ("Switzerland", "CHF"), ("SNB", "CHF"),
                      ("Bank of England", "GBP"), ("BoE", "GBP"))


def _fc_country(name: str) -> str:
    n = (name or "").strip().lower()
    for prefix, cur in _FC_COUNTRY_PREFIX:
        p = prefix.lower()
        if (n.startswith(p) if p.endswith(" ") else p in n):
            return cur
    return "USD"   # Finance Calendar is US-centric; un-prefixed prints (PPI, Jobless claims, PCE…) are US


def parse_financecalendar(payload: dict) -> list:
    """Finance Calendar JSON → the internal event shape (country derived from the name prefix; all-day rows skipped)."""
    out = []
    for e in (payload or {}).get("events") or []:
        if e.get("all_day") or not e.get("time_utc") or not e.get("name"):
            continue
        try:
            when = datetime.fromisoformat(str(e["time_utc"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        impact = str(e.get("impact") or "").lower()
        out.append({"title": str(e["name"]).strip(), "country": _fc_country(e["name"]),
                    "impact": impact if impact in ("high", "medium", "low") else "low",
                    "when": when.isoformat(), "when_ts": when.timestamp(),
                    "forecast": str(e.get("consensus") or ""), "previous": str(e.get("prior") or ""),
                    "source": "financecalendar"})
    return out


async def _fetch_financecalendar() -> list:
    async with httpx.AsyncClient(timeout=20.0, headers=UA, follow_redirects=True) as c:
        r = await c.get(FC_FEED)
        r.raise_for_status()
        return parse_financecalendar(r.json())


def merge_events(primary: list, secondary: list) -> list:
    """Merge by (currency, minute). A secondary print at a time+currency the primary already covers is dropped
    (same freeze window either way); everything else is added. Sorted by time."""
    seen = {(e["country"], int(e["when_ts"] // 60)) for e in primary}
    out = [dict(e, source=e.get("source") or "forexfactory") for e in primary]
    for e in secondary:
        key = (e["country"], int(e["when_ts"] // 60))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return sorted(out, key=lambda x: x["when_ts"])


def failsafe_events(now: float | None = None, days: int = 3) -> list:
    """Synthetic USD HIGH-impact prints at the standard US release times (+18:00 UTC on FOMC days) for the next
    `days` weekdays — used only while every source is > FAILSAFE_AFTER_SEC stale."""
    now = time.time() if now is None else now
    day = datetime.fromtimestamp(now, tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for d in range(days + 1):
        dt = day + timedelta(days=d)
        if dt.weekday() >= 5:
            continue
        times = list(FAILSAFE_TIMES_UTC) + ([(18, 0)] if dt.strftime("%Y-%m-%d") in FOMC_STATEMENT_DAYS else [])
        for h, m in times:
            when = dt.replace(hour=h, minute=m)
            if when.timestamp() + 3600 < now:
                continue
            label = "FOMC statement (fail-safe)" if (h, m) == (18, 0) else f"US {h:02d}:{m:02d} release slot (fail-safe)"
            out.append({"title": label, "country": "USD", "impact": "high", "when": when.isoformat(),
                        "when_ts": when.timestamp(), "forecast": "", "previous": "", "source": "failsafe"})
    return out


async def _refresh_source(name: str, fetch) -> None:
    st = _sources[name]
    try:
        evts = await fetch()
        if evts is None:
            raise RuntimeError("empty response")
        st["events"], st["fetched_at"], st["last_error"] = evts, time.time(), None
    except Exception as e:  # noqa: BLE001
        st["last_error"] = f"{type(e).__name__}: {e}"


def _source_age(name: str, now: float) -> float | None:
    f = _sources[name]["fetched_at"] or 0
    return (now - f) if f else None


def active_source(now: float | None = None) -> str | None:
    """Freshest source that has events, primary first on ties within the stale window."""
    now = time.time() if now is None else now
    best = None
    for name in SOURCE_ORDER:
        st = _sources[name]
        if not st["events"]:
            continue
        age = _source_age(name, now)
        if age is not None and age <= STALE_AFTER_SEC:
            return name
        if best is None or (age or 1e18) < (_source_age(best, now) or 1e18):
            best = name
    return best


def failsafe_active(now: float | None = None) -> bool:
    now = time.time() if now is None else now
    ages = [_source_age(n, now) for n in SOURCE_ORDER]
    return all(a is None or a > FAILSAFE_AFTER_SEC for a in ages)


def _assemble(now: float) -> list:
    ff = _sources["forexfactory"]["events"] or []
    fc = _sources["financecalendar"]["events"] or []
    merged = merge_events(ff, fc)
    if failsafe_active(now):
        merged = merge_events(merged, failsafe_events(now))     # union: stale real prints + conservative defaults
    return merged


async def get_events() -> list:
    """Merged events from both sources (cached 1 h; 10 min back-off after failures). Fail-safe windows are
    added when both sources are > 6 h stale."""
    if time.time() < _cache["expires_at"] and _cache["events"] is not None:
        return _cache["events"]
    async with _lock:
        if time.time() < _cache["expires_at"] and _cache["events"] is not None:
            return _cache["events"]
        await _refresh_source("forexfactory", _fetch_events)
        await _refresh_source("financecalendar", _fetch_financecalendar)
        now = time.time()
        any_ok = any(_sources[n]["last_error"] is None and _sources[n]["events"] is not None for n in SOURCE_ORDER)
        _cache["events"] = _assemble(now)
        _cache["expires_at"] = now + (3600 if any_ok else 600)
        if any_ok:
            _cache["fetched_at"] = now
        _cache["last_error"] = None if any_ok else " | ".join(
            f"{n}: {_sources[n]['last_error']}" for n in SOURCE_ORDER if _sources[n]["last_error"])
        await _persist_status()
        return _cache["events"]


async def _persist_status() -> None:
    """v1.60.39 — Bot Health (API process) reads the calendar status the worker recorded."""
    try:
        from news_status import record_source_status
        await record_source_status("calendar", feed_status())
    except Exception:  # noqa: BLE001
        pass


def feed_status() -> dict:
    """Diagnostics for UIs: per-source health, active source, staleness alert (> 2 h) and fail-safe state (> 6 h)."""
    now = time.time()
    act = active_source(now)
    act_age = _source_age(act, now) if act else None
    fs = failsafe_active(now)
    srcs = {}
    for name in SOURCE_ORDER:
        st = _sources[name]
        age = _source_age(name, now)
        srcs[name] = {"events": len(st["events"] or []), "last_error": st["last_error"],
                      "last_fetch_at": (datetime.fromtimestamp(st["fetched_at"], tz=timezone.utc).isoformat()
                                        if st["fetched_at"] else None),
                      "age_min": round(age / 60.0, 1) if age is not None else None,
                      "status": ("DOWN" if not st["events"] else "STALE" if age is not None and age > STALE_AFTER_SEC
                                 else "DEGRADED" if st["last_error"] else "OK")}
    status = ("FAILSAFE" if fs else "DOWN" if not act else "STALE" if act_age and act_age > STALE_AFTER_SEC
              else "DEGRADED" if any(_sources[n]["last_error"] for n in SOURCE_ORDER) else "OK")
    return {
        "provider": "ForexFactory (weekly XML) + Finance Calendar (JSON)",
        "active_source": act,
        "sources": srcs,
        "stale": bool(act_age is not None and act_age > STALE_AFTER_SEC) or act is None,
        "failsafe_active": fs,
        "stale_after_min": STALE_AFTER_SEC // 60, "failsafe_after_min": FAILSAFE_AFTER_SEC // 60,
        "attribution": FC_ATTRIBUTION,
        "last_fetch_at": (datetime.fromtimestamp(_cache["fetched_at"], tz=timezone.utc).isoformat()
                          if _cache.get("fetched_at") else None),
        "last_fetch_age_min": round((now - _cache["fetched_at"]) / 60.0, 1) if _cache.get("fetched_at") else None,
        "cached_events": len(_cache.get("events") or []),
        "last_error": _cache.get("last_error"),
        "status": status,
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
