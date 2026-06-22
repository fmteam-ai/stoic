from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from economic_calendar import get_events, upcoming_for, macro_freeze_check

router = APIRouter(prefix="/calendar", tags=["calendar"])


@router.get("")
async def all_events(user=Depends(get_current_user)):
    return {"events": await get_events()}


@router.get("/upcoming/{symbol}")
async def upcoming(symbol: str, hours: int = Query(24, ge=1, le=168),
                   user=Depends(get_current_user)):
    return {"symbol": symbol.upper(),
            "hours": hours,
            "events": await upcoming_for(symbol, hours)}


@router.get("/freeze/{symbol}")
async def freeze(symbol: str, user=Depends(get_current_user)):
    return await macro_freeze_check(symbol)
