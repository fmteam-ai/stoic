"""#41 — GET /api/market-intel: read-only Market Intelligence card (auth required, user-scoped, no secrets)."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db
from market_intel import build

router = APIRouter(prefix="/market-intel", tags=["market-intel"])


@router.get("")
async def market_intel(user=Depends(get_current_user)):
    return await build(get_db(), user["id"])


@router.get("/gold-bias")
async def gold_bias_view(user=Depends(get_current_user)):
    """v1.60.60 #71 — latest Gold Daily Bias snapshot + the caller's 2-week shadow tally (read-only, no trading effect)."""
    from gold_bias import latest, shadow_summary
    db = get_db()
    return {"bias": await latest(db), "shadow": await shadow_summary(db, user["id"])}
