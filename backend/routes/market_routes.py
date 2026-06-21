from fastapi import APIRouter, HTTPException, Depends, Query
from typing import List

from auth import get_current_user
from market import get_quote, get_history, compute_indicators, supported_symbols
from risk import PROFILES

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/symbols")
async def list_supported_symbols(user=Depends(get_current_user)):
    return {"symbols": supported_symbols()}


@router.get("/quote/{symbol}")
async def quote(symbol: str, user=Depends(get_current_user)):
    try:
        return await get_quote(symbol)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Market data error: {e}")


@router.get("/quotes")
async def quotes(symbols: str = Query(..., description="Comma-separated symbols"),
                 user=Depends(get_current_user)):
    out = []
    for s in [x.strip() for x in symbols.split(",") if x.strip()]:
        try:
            out.append(await get_quote(s))
        except Exception as e:
            out.append({"symbol": s, "error": str(e)})
    return {"quotes": out}


@router.get("/history/{symbol}")
async def history(symbol: str, user=Depends(get_current_user)):
    try:
        hist = await get_history(symbol)
        indicators = compute_indicators(hist)
        return {"symbol": symbol.upper(), "history": hist, "indicators": indicators}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Market data error: {e}")


@router.get("/risk-profiles")
async def risk_profiles(user=Depends(get_current_user)):
    return {"profiles": PROFILES}
