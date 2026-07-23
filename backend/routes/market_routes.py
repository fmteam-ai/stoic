from fastapi import APIRouter, HTTPException, Depends, Query
from typing import List

from auth import get_current_user
from market import get_quote, get_history, compute_indicators, supported_symbols
from risk import PROFILES

import logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/symbols")
async def list_supported_symbols(user=Depends(get_current_user)):
    return {"symbols": supported_symbols()}


@router.get("/quote/{symbol}")
async def quote(symbol: str, user=Depends(get_current_user)):
    try:
        return await get_quote(symbol)
    except Exception as e:
        from errors import api_error
        raise api_error(502, "market_data_unavailable", "Market data is temporarily unavailable — try again shortly.", exc=e)


@router.get("/quotes")
async def quotes(symbols: str = Query(..., description="Comma-separated symbols"),
                 user=Depends(get_current_user)):
    out = []
    for s in [x.strip() for x in symbols.split(",") if x.strip()]:
        try:
            out.append(await get_quote(s))
        except Exception as e:
            logger.warning("quote fetch failed for %s: %s", s, e)
            out.append({"symbol": s, "error": "quote_unavailable"})
    return {"quotes": out}


@router.get("/history/{symbol}")
async def history(symbol: str, user=Depends(get_current_user)):
    try:
        hist = await get_history(symbol)
        indicators = compute_indicators(hist)
        return {"symbol": symbol.upper(), "history": hist, "indicators": indicators}
    except Exception as e:
        from errors import api_error
        raise api_error(502, "market_data_unavailable", "Market data is temporarily unavailable — try again shortly.", exc=e)


@router.get("/risk-profiles")
async def risk_profiles(user=Depends(get_current_user)):
    return {"profiles": PROFILES}
