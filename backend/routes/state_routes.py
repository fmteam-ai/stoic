"""State contract API (iter-158) — GET /api/state/contract."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/state", tags=["state-contract"])


@router.get("/contract")
async def state_contract(user=Depends(get_current_user)):
    from state_contract import contract
    return await contract(get_db(), user["id"])


@router.get("/readiness")
async def trading_readiness_ep(user=Depends(get_current_user)):
    """Audit F-02 — canonical Trading Readiness (READY / DEGRADED /
    CLOSE_ONLY / BLOCKED / EMERGENCY) with stable reason codes."""
    from trading_readiness import readiness
    return await readiness(get_db(), user["id"])


@router.get("/inventory")
async def state_inventory(user=Depends(get_current_user)):
    """Audit v3 P0-2 — canonical account/bot/EA inventory counters."""
    from state_contract import inventory
    return await inventory(get_db(), user["id"])
