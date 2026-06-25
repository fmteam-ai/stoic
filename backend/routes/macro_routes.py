"""Routes for FRED macro feeds."""
from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from macro_feeds import get_macro_snapshot
from macro_gate import gate_status
from economic_calendar import macro_freeze_check

router = APIRouter(prefix="/macro", tags=["macro"])


@router.get("/snapshot")
async def macro_snapshot(force: bool = Query(False, description="Bypass 1h cache (admin debug only)"),
                        user=Depends(get_current_user)):
    """5 FRED series with latest value + day-over-day + week-over-week deltas,
    plus a top-level summary block ({regime, gate_open, vix, dxy, ...}) so the
    dashboard widget can render a single-line status without fetching /gate.
    """
    snap = await get_macro_snapshot(force_refresh=force)

    # Fold in the current gate state — saves the UI a second round-trip
    # and ensures the snapshot is self-describing.
    try:
        gate = await gate_status()
    except Exception:
        gate = None

    series = snap.get("series") if isinstance(snap, dict) else snap
    by_id = {s["series_id"]: s for s in (series or []) if isinstance(s, dict) and s.get("series_id")}

    def _val(sid: str):
        return (by_id.get(sid) or {}).get("latest")

    summary = {
        # "regime" mirrors gate_status — see macro_gate.py for the taxonomy.
        "regime": (gate or {}).get("regime"),
        "gate_open": (
            (gate or {}).get("buy_ok") is True or (gate or {}).get("sell_ok") is True
        ) if gate is not None else None,
        "buy_ok": (gate or {}).get("buy_ok"),
        "sell_ok": (gate or {}).get("sell_ok"),
        "blocked": (gate or {}).get("blocked") or [],
        # Quick scalars the MacroClimate widget displays directly.
        "vix": _val("VIXCLS"),
        "dxy": _val("DTWEXBGS"),
        "y10y": _val("DGS10"),
        "y2y": _val("DGS2"),
        "evaluated_at": (gate or {}).get("evaluated_at"),
    }
    if isinstance(snap, dict):
        snap["summary"] = summary
        return snap
    return {"series": series, "summary": summary}


@router.get("/gate")
async def macro_gate_status(user=Depends(get_current_user)):
    """Current macro-regime gate status for XAUUSD.

    Returns whether the deterministic gate is open for BUY / SELL based on
    cached 10Y yield + USD broad index moves. Drives the dashboard banner
    that warns users when gold trades are auto-blocked by the Safety
    Guardian's macro check.
    """
    return await gate_status()


@router.get("/freeze/{symbol}")
async def macro_freeze(symbol: str, user=Depends(get_current_user)):
    """Alias for /api/calendar/freeze/{symbol} — frontend MacroClimate
    widget hits /api/macro/freeze/. Same response shape (NFP/FOMC/CPI
    proximity window check) so the dashboard "FROZEN" badge works.
    """
    return await macro_freeze_check(symbol)

