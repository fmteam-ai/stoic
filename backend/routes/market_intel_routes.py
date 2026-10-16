"""#41 — GET /api/market-intel: read-only Market Intelligence card (auth required, user-scoped, no secrets)."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db
from market_intel import build

router = APIRouter(prefix="/market-intel", tags=["market-intel"])


@router.get("")
async def market_intel(user=Depends(get_current_user)):
    return await build(get_db(), user["id"])
