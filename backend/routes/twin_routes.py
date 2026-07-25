"""Tier 6 · Digital Twin — per-account shadow copy vs live results."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/twin", tags=["digital-twin"])


@router.get("/summary")
async def twin(days: int = 30, user=Depends(get_current_user)):
    from digital_twin import twin_summary
    return await twin_summary(get_db(), user["id"],
                              days=min(max(int(days), 1), 90))
