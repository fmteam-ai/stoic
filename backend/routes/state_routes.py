"""State contract API (iter-158) — GET /api/state/contract."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/state", tags=["state-contract"])


@router.get("/contract")
async def state_contract(user=Depends(get_current_user)):
    from state_contract import contract
    return await contract(get_db(), user["id"])
