"""Tier 13 · Strategy Genetics — version lineage and change attribution."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/genetics", tags=["strategy-genetics"])


@router.get("/lineage")
async def genetics_lineage(user=Depends(get_current_user)):
    from strategy_genetics import lineage
    return await lineage(get_db(), user["id"])
