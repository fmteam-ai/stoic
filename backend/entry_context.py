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


async def cached_dxy() -> dict | None:
    """v1.60.39 — CACHE ONLY (dxy_cache._id=latest, refreshed by the signal path / macro worker). The scalp engine
    builds its context from a bare signal, so without this its entry DXY was always null."""
    try:
        from database import get_db
        doc = await get_db().dxy_cache.find_one({"_id": "latest"}, {"current_price": 1, "regime": 1})
        return doc or None
    except Exception as e:  # noqa: BLE001
        logger.debug("entry context: DXY cache unavailable: %s", e)
        return None


async def build_entry_context(signal: dict) -> dict:
    if not isinstance(signal.get("dxy") or signal.get("dxy_dollar_index"), dict):
        dxy = await cached_dxy()
        if dxy:
            signal = {**signal, "dxy": dxy}
    return context_from_signal(signal, await cached_fred_series())
