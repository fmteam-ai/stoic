"""#41 — MARKET INTELLIGENCE CARD (display only).

One read-only, user-scoped view of everything the bot currently "knows" about the market and how each piece
is (or is not) used. Reads ONLY what the agents already store (calendar cache, news_ai_cache, central_bank_releases,
dxy_cache, tips_cache, fred_cache, risk_regime_advisory, trade_decisions, bot_configs). No trading effect, no
raw API payloads, no keys. Every section degrades to {"available": False, "note": …} instead of failing."""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from entry_context import DXY_MAX_AGE_HOURS, dxy_cache_age_hours
from macro.fred import vol_context, vol_series_stale

log = logging.getLogger("market-intel")

INSTRUMENTS = ("EURUSD", "XAUUSD")
EVENT_CCYS = {"USD", "EUR", "GBP", "JPY", "CNY"}          # USD/EUR + the gold-freeze set
HEADLINE_WINDOW_H = 4
CB_RECENT_MIN = 60
REAL_YIELD_MAX_AGE_H = 48
USAGE_RE = re.compile(r"macro|freeze|avalanche|news|asia|off-hours|gold skip|dxy|fed", re.I)
SECRET_KEYS = re.compile(r"token|api_key|apikey|secret|password|authorization", re.I)


def _dt(v) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _age_min(v, now: datetime) -> float | None:
    d = _dt(v)
    return round((now - d).total_seconds() / 60.0, 1) if d else None


def _safe_url(u) -> str | None:
    """Audit #38 P3 — third-party feed links are rendered as hrefs: only http(s) schemes leave the server."""
    u = str(u or "").strip()
    return u if u.lower().startswith(("http://", "https://")) else None


def _unavailable(note: str) -> dict:
    return {"available": False, "note": note}


# ----------------------------------------------------------------------------------------- 1. next events + freeze
def _freeze_line(checks: dict) -> str | None:
    """'Freeze 12:20–12:45 UTC · XAUUSD, EURUSD' for the active or next window, grouped by window."""
    groups: dict = {}
    for sym, fc in checks.items():
        ev = fc.get("event") if fc.get("frozen") else fc.get("next_freeze")
        if not ev:
            continue
        key = (ev.get("freeze_start"), ev.get("freeze_end"))
        groups.setdefault(key, {"active": bool(fc.get("frozen")), "syms": []})["syms"].append(sym)
    if not groups:
        return None
    (start, end), g = sorted(groups.items(), key=lambda kv: (not kv[1]["active"], kv[0][0] or ""))[0]
    s, e = _dt(start), _dt(end)
    day = "" if not s or s.date() == datetime.now(timezone.utc).date() else s.strftime("%a %d %b ")
    when = f"{day}{s.strftime('%H:%M')}–{e.strftime('%H:%M')} UTC" if s and e else "?"
    return f"{'Freeze' if g['active'] else 'Freeze coming'} {when} · {', '.join(g['syms'])}"


def next_events(events: list, checks: dict, feed: dict, now: float) -> dict:
    rel = [e for e in events if e.get("impact") == "high" and e.get("country") in EVENT_CCYS
           and float(e.get("when_ts") or 0) >= now - 90 * 60]
    rel.sort(key=lambda e: e["when_ts"])
    out = []
    for e in rel[:3]:
        secs = int(e["when_ts"] - now)
        released = e.get("actual") not in (None, "")
        out.append({"time_utc": datetime.fromtimestamp(e["when_ts"], tz=timezone.utc).isoformat(),
                    "countdown_sec": secs, "released": released, "currency": e.get("country"),
                    "event": e.get("title"), "forecast": e.get("forecast"), "previous": e.get("previous"),
                    "actual": e.get("actual") if released else None,
                    "surprise": e.get("surprise") if released else None, "source": e.get("source")})
    return {"available": True, "events": out, "freeze": _freeze_line(checks),
            "freeze_active": any(c.get("frozen") for c in checks.values()),
            "calendar_source": feed.get("active_source"), "calendar_status": feed.get("status"),
            "last_refresh": feed.get("last_fetch_at"), "failsafe_active": bool(feed.get("failsafe_active"))}


# ----------------------------------------------------------------------------------------- 2. headlines + bias
def _direction(score) -> str:
    if score is None:
        return "neutral"
    return "bullish" if score >= 0.5 else "bearish" if score <= -0.5 else "neutral"


def top_headlines(news_docs: dict, now: datetime) -> dict:
    """news_docs: {base: news_ai_cache doc}. Merge the per-instrument scored items by title; last 4 h; top 5."""
    merged: dict = {}
    cutoff = now - timedelta(hours=HEADLINE_WINDOW_H)
    for base, doc in news_docs.items():
        # snapshots written before #41 carry only the top-3 drivers (no publishedAt) — show them at snapshot time
        items = (doc or {}).get("items") or [{**d, "publishedAt": doc.get("fetched_at")} for d in (doc or {}).get("drivers") or []]
        for it in items:
            pub = _dt(it.get("publishedAt"))
            if pub is None or pub < cutoff:
                continue
            key = (it.get("title") or "").strip().lower()
            row = merged.setdefault(key, {"time": pub.isoformat(), "source": it.get("source"), "title": it.get("title"),
                                          "url": _safe_url(it.get("url")), "instruments": [], "direction": {}, "relevance": 0.0,
                                          "urgency": "high" if now - pub <= timedelta(hours=1) else "normal",
                                          "why": it.get("why")})
            if base not in row["instruments"]:
                row["instruments"].append(base)
            row["direction"][base] = _direction(it.get("score"))
            row["relevance"] = round(max(row["relevance"], abs(float(it.get("score") or 0))), 1)
    rows = sorted(merged.values(), key=lambda r: (-r["relevance"], r["time"]), reverse=False)[:5]
    bias = {}
    for base in INSTRUMENTS:
        d = news_docs.get(base)
        bias[base] = ({"net": d.get("net"), "label": d.get("label"), "headlines": d.get("headlines"),
                       "fetched_at": d.get("fetched_at"), "age_min": _age_min(d.get("fetched_at"), now)}
                      if d else {"net": None, "label": None, "headlines": 0, "note": "no scored snapshot yet"})
    if not news_docs:
        return {**_unavailable("no scored news snapshot yet"), "headlines": [], "bias": bias}
    return {"available": True, "headlines": rows, "bias": bias, "window_hours": HEADLINE_WINDOW_H}


# ----------------------------------------------------------------------------------------- 3. central banks
def central_banks(releases: list, now: datetime) -> dict:
    if not releases:
        return {**_unavailable("no central-bank releases stored yet"), "banks": []}
    latest: dict = {}
    for r in releases:                       # newest first
        if r.get("bank") in latest:
            continue
        ts = _dt(r.get("published")) or _dt(r.get("seen_at"))
        try:
            from email.utils import parsedate_to_datetime
            ts = parsedate_to_datetime(str(r.get("published"))).astimezone(timezone.utc) if r.get("published") else ts
        except (TypeError, ValueError):
            pass
        latest[r["bank"]] = {"bank": r.get("bank"), "currency": r.get("ccy"), "time": ts.isoformat() if ts else None,
                             "title": r.get("title"), "link": _safe_url(r.get("link")), "kind": r.get("kind"),
                             "recent": bool(ts and now - ts <= timedelta(minutes=CB_RECENT_MIN))}
    return {"available": True, "banks": sorted(latest.values(), key=lambda b: b["time"] or "", reverse=True)}


# ----------------------------------------------------------------------------------------- 4. macro gauges
def macro_gauges(dxy: dict | None, tips: dict | None, series: dict | None, now: datetime) -> dict:
    out = {}
    age_h = dxy_cache_age_hours(dxy, now) if dxy else None
    out["dxy"] = ({"value": dxy.get("current_price"), "regime": dxy.get("regime"), "age_hours": round(age_h, 1) if age_h is not None else None,
                   "stale": age_h is None or age_h > DXY_MAX_AGE_HOURS, "limit_hours": DXY_MAX_AGE_HOURS}
                  if dxy else {"value": None, "stale": True, "note": "not available"})
    if tips and tips.get("real_yield_10y") is not None:
        ry_age = dxy_cache_age_hours(tips, now)
        out["real_yield_10y"] = {"value": tips.get("real_yield_10y"), "change_5d": tips.get("delta_5d"),
                                 "date": tips.get("real_yield_10y_date"), "regime": tips.get("regime"),
                                 "stale": ry_age is None or ry_age > REAL_YIELD_MAX_AGE_H, "source": tips.get("source")}
    else:
        out["real_yield_10y"] = {"value": None, "stale": True, "note": "not available"}
    vol = vol_context(series, now)
    for sid, key in (("GVZCLS", "gvz"), ("VIXCLS", "vix")):
        e = (series or {}).get(sid)
        out[key] = ({"value": e.get("value"), "regime": e.get("vol_regime"), "pct_20d": e.get("pct_20d"),
                     "change_5d": e.get("change_5d"), "date": e.get("date"), "stale": vol_series_stale(e, now)}
                    if e else {"value": None, "stale": True, "note": "not available"})
    out["available"] = any(not v.get("stale") for v in out.values() if isinstance(v, dict))
    out["vol_summary"] = {"gvz_regime": vol.get("gvz_regime"), "vix_regime": vol.get("vix_regime")}
    return out


# ----------------------------------------------------------------------------------------- 5/6. geo + regime
def geo_risk(advisories: list, latest: dict | None) -> dict:
    """advisories: last-7-day docs oldest→newest with at + geo_risk."""
    pts = [{"at": a.get("at"), "v": a.get("geo_risk")} for a in advisories if a.get("geo_risk") is not None]
    if len(pts) > 56:
        step = len(pts) / 56.0
        pts = [pts[int(i * step)] for i in range(56)]
    if not latest or latest.get("geo_risk") is None:
        return {**_unavailable("GDELT geo-risk not computed yet" + (f" ({latest.get('gdelt_error')})" if latest and latest.get("gdelt_error") else "")),
                "sparkline": pts}
    regions = latest.get("regions") or {}
    top = latest.get("top_regions") or sorted(regions, key=lambda k: (regions[k] or {}).get("score") or 0, reverse=True)[:3]
    return {"available": True, "score": latest.get("geo_risk"), "sparkline": pts, "top_regions": list(top)[:3],
            "regions": regions, "at": latest.get("at"), "gdelt_error": latest.get("gdelt_error")}


def gate_label(flag_on: bool) -> str:
    if not flag_on:
        return "SHADOW — not affecting trades"
    return "FLAG ON — no gate behaviour is implemented yet (plumbing only); still not affecting trades"


def risk_regime(latest: dict | None, flag_on: bool) -> dict:
    base = {"gate_enabled": flag_on, "label": gate_label(flag_on), "shadow": True}
    if not latest:
        return {**base, **_unavailable("no advisory computed yet")}
    return {**base, "available": True, "score": latest.get("risk_off_score"), "bucket": latest.get("bucket"),
            "components": latest.get("components"), "top_drivers": latest.get("top_drivers"),
            "missing": latest.get("missing"), "at": latest.get("at")}


# ----------------------------------------------------------------------------------------- 7. usage per instrument
def usage_lines(sym: str, cfg: dict, fc: dict, news_doc: dict | None, decision: dict | None, now: datetime) -> dict:
    from agents.execution_optimizer_agent import _is_xau_off_hours, off_hours_window
    from bot_runner import asia_gold_skip_active
    from news_understanding import news_gate
    frozen = bool(fc.get("frozen"))
    nf = fc.get("next_freeze")
    aval = (fc.get("kind") == "avalanche") or bool(nf and nf.get("freeze_kind") == "avalanche")
    snap = news_doc if news_doc and news_doc.get("net") is not None else None
    gates = {a: news_gate(a, sym, snap) for a in ("BUY", "SELL")} if snap else {}
    out = {"instrument": sym,
           "macro_freeze": {"on": frozen, "why": fc.get("reason") or (f"next: {nf.get('title')} {nf.get('when_human')}" if nf else "no high-impact event in the freeze window")},
           "avalanche_guard": {"on": frozen and fc.get("kind") == "avalanche",
                               "state": "ACTIVE" if frozen and fc.get("kind") == "avalanche" else "ARMED (cluster ahead)" if aval else "idle"},
           "news_bias": ({"net": snap.get("net"), "label": snap.get("label"), "headlines": snap.get("headlines"),
                          "gate": {a: (g or "pass") for a, g in gates.items()}} if snap
                         else {"net": None, "label": None, "note": "no scored snapshot — Strategy agent gets no news bias"}),
           "last_decision": ({"time": decision.get("ts"), "action": decision.get("status"), "stage": decision.get("stage"),
                              "reason": decision.get("reason")} if decision else None)}
    if sym == "XAUUSD":
        win = off_hours_window(cfg)
        asia_on = cfg.get("asia_session_skip_xau", True) is not False
        out["gold_windows"] = {
            "off_hours": {"on": _is_xau_off_hours(now, cfg), "window_utc": (f"{win[0] // 60:02d}:{win[0] % 60:02d}–{win[1] // 60:02d}:{win[1] % 60:02d}" if win else "disabled")},
            "asia_skip": {"enabled": asia_on, "on": asia_on and asia_gold_skip_active(now), "window_utc": "00:00–07:00"}}
    return out


# ----------------------------------------------------------------------------------------- 8. source health
def _row(name: str, status: str, last_success, items_1h, note: str | None = None) -> dict:
    return {"feed": name, "status": status, "last_success": last_success, "items_last_hour": items_1h, "note": note}


def _status_from_age(last, now: datetime, stale_min: float, error: str | None = None) -> str:
    age = _age_min(last, now)
    if error and age is None:
        return "error"
    if age is None:
        return "stale"
    return "stale" if age > stale_min else "ok"


def source_health(persisted: dict, finnhub_cfg: bool, newsapi_cfg: bool, cb_last, cb_1h: int, adv_latest: dict | None,
                  adv_1h: int, fred_at, dxy: dict | None, now: datetime) -> list:
    cal = persisted.get("calendar") or {}
    fh = cal.get("finnhub") or {}
    rss = persisted.get("rss_ai") or {}
    rows = []
    fh_cal = (cal.get("sources") or {}).get("finnhub") or {}
    if not finnhub_cfg:
        rows.append(_row("Finnhub calendar", "not_configured", None, 0, "FINNHUB_API_KEY not set — ForexFactory / financecalendar in use"))
        rows.append(_row("Finnhub news", "not_configured", None, 0, "FINNHUB_API_KEY not set"))
    else:
        unavailable = "calendar/economic" in (fh.get("unavailable") or [])
        rows.append(_row("Finnhub calendar", "unavailable" if unavailable else _status_from_age(fh_cal.get("last_fetch_at"), now, 60, fh_cal.get("last_error")),
                         fh_cal.get("last_fetch_at"), fh_cal.get("events") or 0, "plan-restricted (HTTP 403)" if unavailable else fh.get("last_error")))
        news_at = datetime.fromtimestamp(fh["last_news_at"], tz=timezone.utc).isoformat() if fh.get("last_news_at") else None
        rows.append(_row("Finnhub news", _status_from_age(news_at, now, 30, fh.get("last_error")), news_at, rss.get("finnhub_items") or 0, fh.get("last_error")))
    if not newsapi_cfg:
        rows.append(_row("NewsAPI", "not_configured", None, 0, "NEWSAPI_KEY not set — RSS wire in use"))
    else:
        rows.append(_row("NewsAPI", _status_from_age(rss.get("last_fetch_at"), now, 60, rss.get("last_error")), rss.get("last_fetch_at"), rss.get("newsapi_items") or 0, rss.get("last_error")))
    rows.append(_row("RSS wire + AI scoring", _status_from_age(rss.get("last_scored_at"), now, 60, rss.get("last_llm_error") or rss.get("last_error")),
                     rss.get("last_scored_at"), rss.get("headlines") or 0, rss.get("last_llm_error")))
    rows.append(_row("Central-bank RSS", _status_from_age(cb_last, now, 24 * 60), cb_last, cb_1h, "polled every 2 min; items are rare by nature"))
    g_err = (adv_latest or {}).get("gdelt_error")
    rows.append(_row("GDELT geo-risk", "error" if g_err and (adv_latest or {}).get("geo_risk") is None else _status_from_age((adv_latest or {}).get("at"), now, 45),
                     (adv_latest or {}).get("at"), adv_1h, g_err))
    rows.append(_row("FRED (GVZ/VIX/yields)", _status_from_age(fred_at, now, 12 * 60), fred_at, None, None))
    dxy_age = dxy_cache_age_hours(dxy, now) if dxy else None
    rows.append(_row("DXY", "stale" if dxy_age is None or dxy_age > DXY_MAX_AGE_HOURS else "ok", (dxy or {}).get("fetched_at"), None,
                     None if dxy else "no DXY snapshot"))
    return rows


def scrub(obj):
    """Defence in depth — drop any key that looks like a credential before the payload leaves the server."""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if not SECRET_KEYS.search(str(k))}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


# ----------------------------------------------------------------------------------------- assembly
async def _safe(coro, fallback):
    try:
        return await coro
    except Exception as e:  # noqa: BLE001 — one dead feed must not break the card
        log.debug("market intel section failed: %s", e)
        return fallback


async def build(db, user_id: str) -> dict:
    from economic_calendar import feed_status, get_events, macro_freeze_check
    from news_status import load_source_status, newsapi_configured
    from providers.finnhub import configured as finnhub_configured
    now = datetime.now(timezone.utc)
    now_ts = time.time()

    cfgs = await _safe(db.bot_configs.find({"user_id": user_id}).to_list(length=20), [])
    cfg = next((c for c in cfgs if not c.get("account_id")), cfgs[0] if cfgs else {})
    flag_on = any(bool(c.get("geo_risk_gate_enabled")) for c in cfgs)

    events = await _safe(get_events(), [])
    checks = {s: await _safe(macro_freeze_check(s, cfg), {"frozen": False}) for s in INSTRUMENTS}
    persisted = await _safe(load_source_status(), {})
    feed = persisted.get("calendar") or feed_status()

    news_docs = {}
    for base in INSTRUMENTS:
        d = await _safe(db.news_ai_cache.find_one({"_id": base}), None)
        if d:
            d.pop("_id", None)
            news_docs[base] = d

    releases = await _safe(db.central_bank_releases.find({}, {"_id": 0}).sort("seen_at", -1).limit(60).to_list(length=60), [])
    cb_1h = sum(1 for r in releases if (_age_min(r.get("seen_at"), now) or 1e9) <= 60)
    dxy = await _safe(db.dxy_cache.find_one({"_id": "latest"}, {"current_price": 1, "regime": 1, "fetched_at": 1}), None)
    tips = await _safe(db.tips_cache.find_one({"key": "10Y_REAL"}, {"_id": 0}), None)
    fred = await _safe(db.fred_cache.find_one({"_id": "snapshot"}, {"payload.series": 1, "payload.fetched_at": 1}), None)
    series = ((fred or {}).get("payload") or {}).get("series") or {}
    since7 = (now - timedelta(days=7)).isoformat()
    advisories = await _safe(db.risk_regime_advisory.find({"at": {"$gte": since7}}, {"_id": 0, "at": 1, "geo_risk": 1})
                             .sort("at", 1).to_list(length=2000), [])
    latest_adv = await _safe(db.risk_regime_advisory.find_one({}, {"_id": 0}, sort=[("at", -1)]), None)
    adv_1h = sum(1 for a in advisories if (_age_min(a.get("at"), now) or 1e9) <= 60)

    usage = []
    for sym in INSTRUMENTS:
        dec = await _safe(db.trade_decisions.find_one(
            {"user_id": user_id, "symbol": {"$regex": f"^{sym}", "$options": "i"}, "reason": {"$regex": USAGE_RE.pattern, "$options": "i"}},
            {"_id": 0, "ts": 1, "status": 1, "stage": 1, "reason": 1}, sort=[("ts", -1)]), None)
        try:
            usage.append(usage_lines(sym, cfg, checks[sym], news_docs.get(sym), dec, now))
        except Exception as e:  # noqa: BLE001
            log.debug("usage line failed for %s: %s", sym, e)
            usage.append({"instrument": sym, **_unavailable("not available")})

    payload = {
        "generated_at": now.isoformat(), "refresh_sec": 60, "read_only": True,
        "next_events": next_events(events, checks, feed, now_ts) if events or feed else _unavailable("calendar not available"),
        "headlines": top_headlines(news_docs, now),
        "central_banks": central_banks(releases, now),
        "macro": macro_gauges(dxy, tips, series, now),
        "geo_risk": geo_risk(advisories, latest_adv),
        "risk_regime": risk_regime(latest_adv, flag_on),
        "usage": usage,
        "source_health": source_health(persisted, finnhub_configured(), newsapi_configured(), releases[0].get("seen_at") if releases else None,
                                       cb_1h, latest_adv, adv_1h, ((fred or {}).get("payload") or {}).get("fetched_at"), dxy, now),
    }
    return scrub(payload)
