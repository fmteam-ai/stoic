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


async def build_entry_context(signal: dict) -> dict:
    series = {}
    try:
        from macro.fred import get_macro_snapshot
        series = (await get_macro_snapshot() or {}).get("series") or {}
    except Exception as e:  # noqa: BLE001 — context must never block an entry
        logger.debug("entry context: FRED snapshot unavailable: %s", e)
    return context_from_signal(signal, series)
