"""v1.60.36 — market context stamped on EVERY signal at entry (scalp engines included), so post-mortems can show
DXY / VIX / 10Y / MTF / setup score "at entry" instead of null. Pure read of values the pipeline already computed
(signal.dxy, signal.real_yield_10y, signal.mtf_tiers / mtf_confluence, signal.setup_score) plus the cached FRED
snapshot for VIX and the nominal 10Y. Never raises; missing inputs stay None."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("entry-context")


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def context_from_signal(signal: dict, fred_series: dict | None = None) -> dict:
    fred_series = fred_series or {}
    dxy = signal.get("dxy") or signal.get("dxy_dollar_index") or {}
    tips = signal.get("real_yield_10y") or {}
    tiers = signal.get("mtf_tiers") or {}
    align = tiers.get("alignment") or {}
    conf = signal.get("mtf_confluence") or {}
    gate = signal.get("mtf_gate") or {}
    aplus = signal.get("aplus_confluence") or {}
    ss = signal.get("setup_score") or {}
    dominant = align.get("dominant")
    action = signal.get("action")
    aligned = gate.get("aligned")
    if aligned is None and dominant in ("UP", "DOWN") and action in ("BUY", "SELL"):
        aligned = (dominant == "UP") == (action == "BUY")
    elif aligned is None and conf.get("aligned") is not None:
        aligned = bool(conf.get("aligned"))
    return {
        "dxy": _num(dxy.get("current_price") if isinstance(dxy, dict) else None),
        "dxy_regime": dxy.get("regime") if isinstance(dxy, dict) else None,
        "vix": _num((fred_series.get("VIXCLS") or {}).get("value")),
        "y10y": _num((fred_series.get("DGS10") or {}).get("value")),
        "real_yield_10y": _num(tips.get("real_yield_10y") if isinstance(tips, dict) else None),
        "mtf": {"dominant": dominant, "aligned": aligned,
                "confluence_aligned": conf.get("aligned") if isinstance(conf, dict) else None,
                "evaluated": bool(tiers or conf or gate)},
        "aplus": {"passed": aplus.get("passed") if isinstance(aplus, dict) else None,
                  "evaluated": bool(aplus), "setup_score": _num(ss.get("score") if isinstance(ss, dict) else None)},
        "engine": signal.get("strategy_engine") or signal.get("scope"),
        "at": datetime.now(timezone.utc).isoformat(),
    }


async def cached_fred_series() -> dict:
    """v1.60.37 — CACHE ONLY. The signal path must never wait on a FRED fetch; the macro worker refreshes the
    snapshot. A missing/expired cache simply yields None context values."""
    try:
        from database import get_db
        doc = await get_db().fred_cache.find_one({"_id": "snapshot"}, {"payload.series": 1})
        return ((doc or {}).get("payload") or {}).get("series") or {}
    except Exception as e:  # noqa: BLE001 — context must never block an entry
        logger.debug("entry context: FRED cache unavailable: %s", e)
        return {}


DXY_MAX_AGE_HOURS = 48          # the CSV is daily — 48 h tolerates a weekend without flagging Monday morning


def dxy_cache_age_hours(doc: dict | None, now: datetime | None = None) -> float | None:
    """Hours since `fetched_at`; None when the cache is missing or has no parsable timestamp (treated as stale)."""
    if not doc or not doc.get("fetched_at"):
        return None
    try:
        fetched = datetime.fromisoformat(str(doc["fetched_at"]).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - fetched).total_seconds() / 3600.0


def dxy_cache_is_stale(doc: dict | None, now: datetime | None = None) -> bool:
    age = dxy_cache_age_hours(doc, now)
    return age is None or age > DXY_MAX_AGE_HOURS


async def cached_dxy() -> dict | None:
    """v1.60.39 — CACHE ONLY (dxy_cache._id=latest, refreshed by the signal path / macro worker). The scalp engine
    builds its context from a bare signal, so without this its entry DXY was always null.
    v1.60.51 — the cache is only trusted for DXY_MAX_AGE_HOURS: macro/dxy.get_dxy_snapshot keeps returning the old
    document when the download fails, so without an age check a week-old regime would be stamped on new entries.
    Older → None (entry stamped "DXY unknown") + one WARNING per hour."""
    try:
        from database import get_db
        doc = await get_db().dxy_cache.find_one({"_id": "latest"}, {"current_price": 1, "regime": 1, "fetched_at": 1})
        if not doc:
            return None
        age = dxy_cache_age_hours(doc)
        if age is None or age > DXY_MAX_AGE_HOURS:
            global _last_stale_warn
            now_m = datetime.now(timezone.utc).timestamp()
            if now_m - _last_stale_warn > 3600:
                _last_stale_warn = now_m
                logger.warning("DXY cache stale: fetched_at=%s age=%sh (> %dh) — entries stamped DXY unknown",
                               doc.get("fetched_at"), None if age is None else round(age, 1), DXY_MAX_AGE_HOURS)
            return None
        return {"current_price": doc.get("current_price"), "regime": doc.get("regime")}
    except Exception as e:  # noqa: BLE001
        logger.debug("entry context: DXY cache unavailable: %s", e)
        return None


_last_stale_warn = 0.0


async def build_entry_context(signal: dict) -> dict:
    if not isinstance(signal.get("dxy") or signal.get("dxy_dollar_index"), dict):
        dxy = await cached_dxy()
        if dxy:
            signal = {**signal, "dxy": dxy}
    fred = await cached_fred_series()
    ctx = context_from_signal(signal, fred)
    # v1.60.53 — SHADOW stamps: risk-off advisory + GVZ/VIX regimes (never read by any gate)
    try:
        from macro.fred import vol_context
        ctx.update({k: v for k, v in vol_context(fred).items() if k.endswith("_regime")})
        from database import get_db
        from risk_regime import latest_advisory, stamp_for_entry
        ctx["risk_regime"] = stamp_for_entry(await latest_advisory(get_db()))
    except Exception as e:  # noqa: BLE001 — context must never block an entry
        logger.debug("entry context: risk regime unavailable: %s", e)
    return ctx
