"""v1.60.53 (B) — ADVISORY / SHADOW ONLY risk regime: GDELT geo-risk, central-bank press releases, GVZ/VIX regime
→ `risk_regime_advisory` docs + a combined risk_off_score 0–100. It logs, scores and displays; it NEVER blocks,
opens, closes or sizes a trade. `geo_risk_gate_enabled` (BotConfig, default False) is plumbing only."""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import httpx

log = logging.getLogger("risk_regime")

GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_INTERVAL_SEC = 15 * 60
GDELT_MIN_GAP_SEC = 5.5                 # GDELT rate limit: one request per 5 s
CB_INTERVAL_SEC = 120
GEO_HISTORY_DAYS = 30
REGIONS = {"global": "(war OR conflict OR military OR sanctions OR terror OR missile)",
           "middle_east": "(Iran OR Israel OR Gaza OR Hezbollah OR Houthi OR \"Red Sea\") (war OR strike OR missile OR attack)",
           "russia_ukraine": "(Russia OR Ukraine OR Kremlin) (war OR strike OR missile OR sanctions)",
           "us_china": "(China OR Beijing) (tariff OR sanctions OR export controls OR trade war)",
           "taiwan": "(Taiwan) (military OR drills OR blockade OR invasion OR PLA)"}
CB_FEEDS = {"USD": ("Fed", "https://www.federalreserve.gov/feeds/press_all.xml"),
            "EUR": ("ECB", "https://www.ecb.europa.eu/rss/press.html"),
            "GBP": ("BoE", "https://www.bankofengland.co.uk/rss/news"),
            "JPY": ("BoJ", "https://www.boj.or.jp/en/rss/whatsnew.xml"),
            "CHF": ("SNB", "https://www.snb.ch/public/en/rss/pressreleases"),
            "AUD": ("RBA", "https://www.rba.gov.au/rss/rss-cb-media-releases.xml"),
            "CAD": ("BoC", "https://www.bankofcanada.ca/content_type/press-releases/feed/")}
CB_DECISION_WORDS = ("rate decision", "monetary policy", "interest rate", "policy rate", "cash rate", "bank rate",
                     "fomc statement", "monetary policy decision", "press conference", "unscheduled", "emergency")
CB_ACTIVE_MINUTES = 90
VOL_SCORE = {None: 0.0, "calm": 0.0, "normal": 20.0, "elevated": 55.0, "stressed": 90.0}
WEIGHTS = {"geo_risk": 0.40, "vix": 0.25, "gvz": 0.20, "cb_event": 0.15}


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def zscore(value: float, history: list[float]) -> float | None:
    hist = [float(h) for h in history if h is not None]
    if len(hist) < 8:
        return None
    mean = sum(hist) / len(hist)
    sd = math.sqrt(sum((h - mean) ** 2 for h in hist) / (len(hist) - 1))
    return None if sd <= 0 else (value - mean) / sd


def geo_component(volume: int, avg_tone: float | None, history: list[dict]) -> dict:
    """Pure — region score 0–100 from a volume z-score against the region's own 30-day baseline plus tone.
    Raw counts alone are never the score (feed volume drifts with GDELT coverage)."""
    z = zscore(float(volume), [h.get("volume") for h in history])
    base = 50.0 if z is None else _clamp(50.0 + 20.0 * z)
    tone_adj = 0.0 if avg_tone is None else _clamp(-avg_tone * 4.0, -20.0, 20.0)   # GDELT tone: negative = hostile
    return {"volume": volume, "avg_tone": avg_tone, "z": None if z is None else round(z, 2),
            "score": round(_clamp(base + tone_adj), 1), "baseline_n": len(history)}


def parse_gdelt_artlist(payload: dict) -> tuple[int, float | None]:
    arts = (payload or {}).get("articles") or []
    tones = []
    for a in arts:
        try:
            tones.append(float(a.get("tone")))
        except (TypeError, ValueError):
            continue
    return len(arts), (round(sum(tones) / len(tones), 2) if tones else None)


async def poll_gdelt(db) -> dict:
    """Every 15 min: article volume + tone per region over the last 15 min; history kept 30 days in geo_risk_history."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=GEO_HISTORY_DAYS)).isoformat()
    out = {"regions": {}, "at": now.isoformat()}
    rate_limited = 0
    async with httpx.AsyncClient(timeout=15) as c:
        for i, (region, query) in enumerate(REGIONS.items()):
            if rate_limited >= 2:
                break                                            # shared egress throttled — try again next cycle
            if i:
                await asyncio.sleep(GDELT_MIN_GAP_SEC)          # GDELT: "limit requests to one every 5 seconds"
            try:
                r = await c.get(GDELT_DOC, params={"query": query, "mode": "artlist", "format": "json",
                                                   "timespan": "15min", "maxrecords": 250})
                if r.status_code == 429:
                    rate_limited += 1
                    _state["last_error"] = "GDELT 429 (rate limited)"
                    continue
                r.raise_for_status()
                vol, tone = parse_gdelt_artlist(r.json())
                _state["last_error"] = None
            except Exception as e:  # noqa: BLE001
                log.debug("gdelt %s failed: %s", region, e)
                _state["last_error"] = f"GDELT {type(e).__name__}"
                continue
            hist = await db.geo_risk_history.find({"region": region, "at": {"$gte": since}}, {"volume": 1}).to_list(length=3000)
            comp = geo_component(vol, tone, hist)
            out["regions"][region] = comp
            await db.geo_risk_history.insert_one({"region": region, "at": now.isoformat(), "volume": vol, "avg_tone": tone,
                                                  "expires_at": now + timedelta(days=GEO_HISTORY_DAYS)})
    if out["regions"]:
        scores = [c["score"] for c in out["regions"].values()]
        out["geo_risk"] = round(0.5 * out["regions"].get("global", {}).get("score", 50.0) + 0.5 * max(scores), 1)
        out["top_regions"] = sorted(out["regions"].items(), key=lambda kv: kv[1]["score"], reverse=True)[:3]
    return out


def parse_cb_rss(xml_text: str, ccy: str, bank: str) -> list:
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for it in root.iter():
        if it.tag.split("}")[-1] not in ("item", "entry"):
            continue
        title = (it.findtext("title") or it.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not link:
            ln = it.find("{http://www.w3.org/2005/Atom}link")
            link = (ln.get("href") if ln is not None else "") or ""
        pub = (it.findtext("pubDate") or it.findtext("{http://www.w3.org/2005/Atom}updated")
               or it.findtext("{http://www.w3.org/2005/Atom}published") or "").strip()
        if not title:
            continue
        low = title.lower()
        items.append({"ccy": ccy, "bank": bank, "title": title[:300], "link": link[:500], "published": pub,
                      "kind": ("decision" if any(w in low for w in CB_DECISION_WORDS) else "release"),
                      "unscheduled": bool(re.search(r"unscheduled|emergency|extraordinary", low))})
    return items


async def poll_central_banks(db) -> list:
    """Every 2 min: title/link/time only — no full-text scraping. New items are upserted by link."""
    new = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        for ccy, (bank, url) in CB_FEEDS.items():
            try:
                r = await c.get(url)
                r.raise_for_status()
                items = parse_cb_rss(r.text, ccy, bank)
            except Exception as e:  # noqa: BLE001
                log.debug("cb feed %s failed: %s", bank, e)
                continue
            for it in items[:20]:
                key = it["link"] or f"{bank}:{it['title']}"
                res = await db.central_bank_releases.update_one(
                    {"_id": key}, {"$setOnInsert": {**it, "seen_at": datetime.now(timezone.utc).isoformat()}}, upsert=True)
                if getattr(res, "upserted_id", None) is not None:
                    new.append(it)
    return new


async def cb_event_active(db, now: datetime | None = None) -> dict | None:
    """Most recent decision-type release seen within CB_ACTIVE_MINUTES → {ccy, bank, title, minutes_since}."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(minutes=CB_ACTIVE_MINUTES)).isoformat()
    rows = await db.central_bank_releases.find({"kind": "decision", "seen_at": {"$gte": since}}).sort("seen_at", -1).to_list(length=1)
    if not rows:
        return None
    r = rows[0]
    seen = datetime.fromisoformat(str(r["seen_at"]).replace("Z", "+00:00"))
    return {"ccy": r["ccy"], "bank": r["bank"], "title": r["title"], "minutes_since": int((now - seen).total_seconds() // 60),
            "unscheduled": bool(r.get("unscheduled"))}


def risk_off_score(geo_risk: float | None, vix_regime: str | None, gvz_regime: str | None, cb_event: dict | None) -> dict:
    """Pure — combined 0–100 with components. Missing inputs contribute 0 and are listed in `missing`."""
    comps = {"geo_risk": None if geo_risk is None else round(float(geo_risk), 1),
             "vix": VOL_SCORE.get(vix_regime, 0.0) if vix_regime else None,
             "gvz": VOL_SCORE.get(gvz_regime, 0.0) if gvz_regime else None,
             "cb_event": None}
    if cb_event:
        decay = max(0.0, 1.0 - cb_event.get("minutes_since", 0) / CB_ACTIVE_MINUTES)
        comps["cb_event"] = round(100.0 * decay * (1.3 if cb_event.get("unscheduled") else 1.0), 1)
    score = sum(WEIGHTS[k] * (v or 0.0) for k, v in comps.items())
    drivers = sorted(((k, round(WEIGHTS[k] * (v or 0.0), 1)) for k, v in comps.items()), key=lambda kv: kv[1], reverse=True)
    return {"risk_off_score": round(_clamp(score), 1), "components": comps,
            "top_drivers": [k for k, _ in drivers[:3]], "missing": [k for k, v in comps.items() if v is None],
            "bucket": bucket(score)}


def bucket(score: float | None) -> str | None:
    if score is None:
        return None
    return "low" if score < 35 else "med" if score < 65 else "high"


async def build_advisory(db, geo: dict | None = None) -> dict:
    """One timestamped `risk_regime_advisory` doc per run (shadow only)."""
    from macro.fred import vol_context
    fred = await db.fred_cache.find_one({"_id": "snapshot"}, {"payload.series": 1})
    vol = vol_context(((fred or {}).get("payload") or {}).get("series"))
    if geo is None:
        geo = await db.risk_regime_advisory.find_one({}, sort=[("at", -1)]) or {}
        geo = {"geo_risk": geo.get("geo_risk"), "regions": geo.get("regions") or {}, "top_regions": geo.get("top_regions")}
    cb = await cb_event_active(db)
    combined = risk_off_score(geo.get("geo_risk"), vol.get("vix_regime"), vol.get("gvz_regime"), cb)
    doc = {"at": datetime.now(timezone.utc).isoformat(), "shadow": True, "geo_risk": geo.get("geo_risk"),
           "gdelt_error": _state.get("last_error"),
           "regions": geo.get("regions") or {}, "top_regions": geo.get("top_regions"), "cb_event_active": cb,
           **vol, **combined}
    await db.risk_regime_advisory.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


async def latest_advisory(db) -> dict | None:
    doc = await db.risk_regime_advisory.find_one({}, sort=[("at", -1)])
    if doc:
        doc.pop("_id", None)
    return doc


def stamp_for_entry(adv: dict | None) -> dict:
    """What entry_context carries (shadow): score, bucket, components, gvz/vix regimes, advisory timestamp."""
    if not adv:
        return {"risk_off_score": None, "risk_off_bucket": None, "evaluated": False}
    return {"risk_off_score": adv.get("risk_off_score"), "risk_off_bucket": adv.get("bucket"),
            "components": adv.get("components"), "gvz_regime": adv.get("gvz_regime"), "vix_regime": adv.get("vix_regime"),
            "cb_event": (adv.get("cb_event_active") or {}).get("ccy"), "at": adv.get("at"), "evaluated": True}


def bucket_evaluation(trades: list) -> list:
    """v1.60.53 (B6) — closed trades grouped by entry risk_off bucket: n, win rate, expectancy R. Pure."""
    groups: dict = {"low": [], "med": [], "high": [], "unknown": []}
    for t in trades:
        b = (((t.get("entry_context") or {}).get("risk_regime") or {}).get("risk_off_bucket")) or "unknown"
        r = t.get("r_multiple")
        if r is None:
            pnl, risk = t.get("pnl"), t.get("risk_usd") or t.get("risk_amount")
            r = (float(pnl) / float(risk)) if pnl is not None and risk else None
        groups.setdefault(b, []).append((float(t.get("pnl") or 0), r))
    out = []
    for b in ("low", "med", "high", "unknown"):
        rows = groups.get(b) or []
        rs = [r for _, r in rows if r is not None]
        out.append({"bucket": b, "n": len(rows),
                    "win_rate": round(100.0 * sum(1 for p, _ in rows if p > 0) / len(rows), 1) if rows else None,
                    "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None})
    return out


_last = {"gdelt": 0.0, "cb": 0.0}
_state: dict = {"last_error": None}


async def tick(db) -> dict | None:
    """Worker entry: GDELT every 15 min, central banks every 2 min; an advisory doc after each GDELT poll."""
    now = time.time()
    geo = None
    if now - _last["cb"] >= CB_INTERVAL_SEC:
        _last["cb"] = now
        try:
            await poll_central_banks(db)
        except Exception as e:  # noqa: BLE001
            log.warning("central bank poll failed: %s", e)
    if now - _last["gdelt"] >= GDELT_INTERVAL_SEC:
        _last["gdelt"] = now
        try:
            geo = await poll_gdelt(db)
        except Exception as e:  # noqa: BLE001
            log.warning("gdelt poll failed: %s", e)
        return await build_advisory(db, geo if geo and geo.get("regions") else None)
    return None
