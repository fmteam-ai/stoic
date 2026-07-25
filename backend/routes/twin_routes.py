"""Tier 6 · Digital Twin — per-account shadow copy vs live results."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db
from entitlements import require_feature

router = APIRouter(prefix="/twin", tags=["digital-twin"],
                   dependencies=[Depends(require_feature("digital_twin"))])


@router.get("/summary")
async def twin(days: int = 30, user=Depends(get_current_user)):
    from digital_twin import twin_summary
    return await twin_summary(get_db(), user["id"],
                              days=min(max(int(days), 1), 90))


@router.get("/stress")
async def twin_stress(days: int = 30, user=Depends(get_current_user)):
    """Phase 1.3 — adverse-execution scenarios on the twin's decision set."""
    from twin_stress import stress
    return await stress(get_db(), user["id"],
                        days=min(max(int(days), 1), 90))
