"""Tier 11 · AI Coach — proactive explanations grounded in recorded state."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/coach", tags=["ai-coach"])


@router.get("/cards")
async def get_coach_cards(user=Depends(get_current_user)):
    from coach import coach_cards
    return await coach_cards(get_db(), user["id"])
