"""Analytics routes — performance attribution endpoints."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from analytics import compute_attribution

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/attribution")
async def get_attribution(user=Depends(get_current_user)):
    """Full performance attribution across every dimension."""
    return await compute_attribution(user["id"])
