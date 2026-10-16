"""v1.60.53 (A) — Finnhub provider: economic calendar + forex/general news. ADVISORY data source.

Key: FINNHUB_API_KEY or FINNHUB_API_KEY_FILE (never logged, never returned). Missing key → provider disabled
(Bot Health: "Finnhub: not configured", info). Calendar countries map to ISO currencies EXPLICITLY — an unknown country
is kept as "OTHER", never USD/ALL (root cause of the 15 Oct gold freeze on UK GDP). 403 / plan-restricted endpoints are
logged once and marked unavailable; 429 backs off exponentially (60 s → 15 min)."""
from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("providers.finnhub")

BASE = "https://finnhub.io/api/v1"
COUNTRY_TO_CCY = {"US": "USD", "EU": "EUR", "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR",
                  "GB": "GBP", "UK": "GBP", "JP": "JPY", "AU": "AUD", "CA": "CAD", "CH": "CHF", "NZ": "NZD",
                  "CN": "CNY"}
IMPACT = {3: "high", 2: "medium", 1: "low", "high": "high", "medium": "medium", "low": "low"}
CALENDAR_INTERVAL_SEC = 300
CALENDAR_HOT_INTERVAL_SEC = 30          # within ±10 min of a HIGH-impact event
CALENDAR_HOT_WINDOW_SEC = 600
NEWS_INTERVAL_SEC = 120
XAU_WORDS = ("gold", "xau", "bullion", "precious metal")
CCY_WORDS = {"USD": ("dollar", "fed ", "fomc", "treasury", "u.s.", "us economy", "powell"),
             "EUR": ("euro", "ecb", "eurozone", "lagarde", "bundesbank"),
             "GBP": ("pound", "sterling", "boe", "bank of england", "gilt"),
             "JPY": ("yen", "boj", "bank of japan"), "AUD": ("aussie", "rba", "australian dollar"),
             "CAD": ("loonie", "bank of canada", "boc"), "CHF": ("franc", "snb", "swiss national bank"),
             "NZD": ("kiwi", "rbnz"), "CNY": ("yuan", "pboc", "renminbi")}
RELEVANCE_WORDS = ("rate", "inflation", "cpi", "gdp", "payroll", "jobs", "central bank", "tariff", "sanction",
                   "war", "oil", "yield", "recession", "fx", "forex", "currency") + XAU_WORDS

_state: dict = {"backoff_until": 0.0, "unavailable": {}, "last_calendar_at": 0.0, "last_news_at": 0.0,
                "last_error": None, "calendar_count": 0, "news_count": 0}


def api_key() -> str | None:
    """Env wins; else *_FILE. Never log the value."""
    key = (os.environ.get("FINNHUB_API_KEY") or "").strip()
    if key:
        return key
    path = (os.environ.get("FINNHUB_API_KEY_FILE") or "").strip()
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            return None
    return None


def configured() -> bool:
    return bool(api_key())


def country_to_ccy(country: str | None) -> str:
    return COUNTRY_TO_CCY.get((country or "").strip().upper(), "OTHER")


def _num(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def surprise(actual, forecast, history: list | None = None) -> dict | None:
    """(actual − forecast) / |forecast|, or a z-score vs the history of past surprises when ≥ 5 exist.
    None until the actual is released. Advisory only."""
    a, f = _num(actual), _num(forecast)
    if a is None or f is None:
        return None
    raw = a - f
    pct = raw / abs(f) if f else None
    out = {"raw": round(raw, 6), "pct": None if pct is None else round(pct, 4), "method": "pct"}
    hist = [h for h in (history or []) if isinstance(h, (int, float))]
    if len(hist) >= 5:
        mean = sum(hist) / len(hist)
        var = sum((h - mean) ** 2 for h in hist) / (len(hist) - 1)
        sd = var ** 0.5
        if sd > 0:
            out["z"], out["method"] = round((raw - mean) / sd, 3), "z"
    return out


def parse_calendar(payload: dict) -> list:
    """Finnhub rows → calendar events in economic_calendar's shape (+ actual, surprise, source)."""
    out = []
    for row in (payload or {}).get("economicCalendar") or []:
        title = str(row.get("event") or "").strip()
        ts = str(row.get("time") or "").strip()
        if not title or not ts:
            continue
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        actual, forecast, previous = row.get("actual"), row.get("estimate"), row.get("prev")
        out.append({"title": title, "country": country_to_ccy(row.get("country")),
                    "country_code": str(row.get("country") or "").upper(),
                    "impact": IMPACT.get(row.get("impact"), "low"),
                    "when": when.isoformat(), "when_ts": when.timestamp(),
                    "forecast": "" if forecast is None else str(forecast),
                    "previous": "" if previous is None else str(previous),
                    "actual": "" if actual is None else str(actual),
                    "surprise": surprise(actual, forecast), "source": "finnhub"})
    return sorted(out, key=lambda e: e["when_ts"])


def calendar_interval_sec(events: list, now: float | None = None) -> int:
    """30 s within ±10 min of a high-impact event, else 5 min."""
    now = now or time.time()
    hot = any(e.get("impact") == "high" and abs(float(e.get("when_ts") or 0) - now) <= CALENDAR_HOT_WINDOW_SEC
              for e in events or [])
    return CALENDAR_HOT_INTERVAL_SEC if hot else CALENDAR_INTERVAL_SEC


def news_fingerprint(item: dict) -> str:
    """URL (or normalised title) hash — dedupe against NewsAPI/RSS items."""
    key = (item.get("url") or "").strip().lower() or " ".join(str(item.get("title") or item.get("headline") or "").lower().split())
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def tag_currencies(text: str) -> list:
    low = (text or "").lower()
    tags = [ccy for ccy, words in CCY_WORDS.items() if any(w in low for w in words)]
    if any(w in low for w in XAU_WORDS):
        tags.append("XAU")
    return tags


def relevant(item: dict) -> bool:
    """Cheap pre-filter so the LLM only scores FX/gold-relevant headlines."""
    text = f"{item.get('headline') or item.get('title') or ''} {item.get('summary') or ''}".lower()
    return bool(tag_currencies(text)) or any(w in text for w in RELEVANCE_WORDS)


def parse_news(rows: list, category: str) -> list:
    out = []
    for r in rows or []:
        title = str(r.get("headline") or "").strip()
        if not title:
            continue
        dt = r.get("datetime")
        published = (datetime.fromtimestamp(float(dt), tz=timezone.utc).isoformat() if dt else None)
        item = {"title": title, "summary": str(r.get("summary") or "")[:500], "url": r.get("url"),
                "source": f"finnhub:{r.get('source') or category}", "category": category,
                "published_at": published, "provider": "finnhub"}
        item["fingerprint"] = news_fingerprint(item)
        item["currencies"] = tag_currencies(f"{title} {item['summary']}")
        item["relevant"] = relevant(item)
        out.append(item)
    return out


def dedupe(items: list, known_fingerprints: set) -> list:
    out, seen = [], set(known_fingerprints or ())
    for it in items:
        fp = it.get("fingerprint") or news_fingerprint(it)
        if fp in seen:
            continue
        seen.add(fp)
        out.append(it)
    return out


def _mark_unavailable(endpoint: str, status: int) -> None:
    if endpoint not in _state["unavailable"]:
        log.warning("Finnhub %s unavailable (HTTP %s — plan restricted?) — source marked unavailable, "
                    "existing sources kept", endpoint, status)
    _state["unavailable"][endpoint] = {"status": status, "at": time.time()}


async def _get(endpoint: str, params: dict) -> dict | list | None:
    key = api_key()
    if not key:
        return None
    if time.time() < _state["backoff_until"]:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{BASE}/{endpoint}", params={**params, "token": key})
    except httpx.HTTPError as e:
        _state["last_error"] = f"{type(e).__name__}"
        return None
    if r.status_code == 429:
        prev = max(60.0, _state.get("backoff_sec", 30.0) * 2)
        _state["backoff_sec"] = min(prev, 900.0)
        _state["backoff_until"] = time.time() + _state["backoff_sec"]
        _state["last_error"] = "rate limited (429)"
        return None
    if r.status_code in (401, 403):
        _mark_unavailable(endpoint, r.status_code)
        return None
    if r.status_code != 200:
        _state["last_error"] = f"HTTP {r.status_code}"
        return None
    _state["backoff_sec"] = 30.0
    _state["last_error"] = None
    _state["unavailable"].pop(endpoint, None)
    return r.json()


async def fetch_calendar(days_back: int = 1, days_ahead: int = 7) -> list | None:
    """None when disabled / unavailable / failed (caller keeps its other sources)."""
    if not configured() or "calendar/economic" in _state["unavailable"]:
        return None
    today = datetime.now(timezone.utc).date()
    payload = await _get("calendar/economic", {"from": (today - timedelta(days=days_back)).isoformat(),
                                               "to": (today + timedelta(days=days_ahead)).isoformat()})
    if payload is None:
        return None
    events = parse_calendar(payload)
    _state["last_calendar_at"], _state["calendar_count"] = time.time(), len(events)
    return events


async def fetch_news() -> list:
    if not configured():
        return []
    items: list = []
    for cat in ("forex", "general"):
        if f"news:{cat}" in _state["unavailable"]:
            continue
        rows = await _get("news", {"category": cat})
        if rows is None:
            continue
        items += parse_news(rows, cat)
    if items:
        _state["last_news_at"], _state["news_count"] = time.time(), len(items)
    return items


def status() -> dict:
    """Bot Health / ops: never includes the key."""
    if not configured():
        return {"configured": False, "level": "info", "message": "Finnhub: not configured (FINNHUB_API_KEY)"}
    return {"configured": True, "level": "ok" if not _state["last_error"] else "warn",
            "last_error": _state["last_error"], "unavailable": sorted(_state["unavailable"]),
            "backoff_until": _state["backoff_until"] or None,
            "last_calendar_at": _state["last_calendar_at"] or None, "calendar_events": _state["calendar_count"],
            "last_news_at": _state["last_news_at"] or None, "news_items": _state["news_count"]}
