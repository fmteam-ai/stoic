"""Tier 5 · Strategy Marketplace — installable strategy cards."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/marketplace", tags=["marketplace"])


@router.get("/strategies")
async def marketplace_strategies(days: int = 90,
                                 user=Depends(get_current_user)):
    from marketplace import strategy_cards
    return await strategy_cards(get_db(), user["id"],
                                days=min(max(int(days), 7), 365))
