"""Tier 13 · Strategy Genetics — version lineage and change attribution."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db
from entitlements import require_feature

router = APIRouter(prefix="/genetics", tags=["strategy-genetics"],
                   dependencies=[Depends(require_feature("strategy_evolution"))])


@router.get("/lineage")
async def genetics_lineage(user=Depends(get_current_user)):
    from strategy_genetics import lineage
    return await lineage(get_db(), user["id"])
