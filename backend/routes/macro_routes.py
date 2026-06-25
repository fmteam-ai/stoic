"""Routes for FRED macro feeds."""
from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from macro_feeds import get_macro_snapshot

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
