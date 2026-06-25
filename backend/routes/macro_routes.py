"""Routes for FRED macro feeds."""
from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from macro_feeds import get_macro_snapshot
from macro_gate import gate_status

router = APIRouter(prefix="/macro", tags=["macro"])


@router.get("/snapshot")
async def macro_snapshot(force: bool = Query(False, description="Bypass 1h cache (admin debug only)"),
                        user=Depends(get_current_user)):
    """5 FRED series with latest value + day-over-day + week-over-week deltas.

    Cached 1h server-side so total FRED traffic stays under 5 calls/hour regardless
    of user count. Use ?force=true to bypass cache (rate-limited by the underlying
    httpx call — don't spam).
    """
    snap = await get_macro_snapshot(force_refresh=force)
    return snap


@router.get("/gate")
async def macro_gate_status(user=Depends(get_current_user)):
    """Current macro-regime gate status for XAUUSD.

    Returns whether the deterministic gate is open for BUY / SELL based on
    cached 10Y yield + USD broad index moves. Drives the dashboard banner
    that warns users when gold trades are auto-blocked by the Safety
    Guardian's macro check.
    """
    return await gate_status()

