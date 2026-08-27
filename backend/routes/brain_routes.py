"""STOIC Brain API — /api/brain (Phase A: regime 2.0, router,
uncertainty, meta decisions, portfolio factor brain)."""
from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/brain", tags=["brain"])


@router.get("/regime")
async def regime_state_ep(symbol: str = Query("XAUUSD"),
                          account_id: str | None = None,
                          user=Depends(get_current_user)):
    from regime_intelligence import market_state
    return await market_state(get_db(), user["id"], symbol,
                              account_id=account_id)


@router.get("/router")
async def router_ep(symbol: str = Query("XAUUSD"),
                    user=Depends(get_current_user)):
    from regime_intelligence import market_state
    from strategy_router import route
    db = get_db()
    state = await market_state(db, user["id"], symbol)
    fp = state.get("fingerprint_key") or "UNKNOWN"
    routing = await route(db, user["id"], fp)
    return {"market_state": state, **routing}


@router.get("/meta/recent")
async def meta_recent_ep(limit: int = 20,
                         user=Depends(get_current_user)):
    db = get_db()
    q = {} if user.get("role") == "admin" else {"user_id": user["id"]}
    lim = max(1, min(int(limit), 100))
    return {"decisions": [d async for d in db.meta_decisions.find(
        q, {"_id": 0}).sort("at", -1).limit(lim)]}


@router.get("/portfolio")
async def portfolio_brain_ep(account_id: str,
                             user=Depends(get_current_user)):
    import portfolio_risk
    db = get_db()
    acct_q = {"user_id": user["id"]} if user.get("role") != "admin" else {}
    from route_utils import parse_object_id
    acc = await db.accounts.find_one(
        {"_id": parse_object_id(account_id), **acct_q})
    equity = float((acc or {}).get("equity") or 0)
    snap = await portfolio_risk.snapshot(db, account_id, equity)
    snap["factors"] = portfolio_risk.factor_exposure(
        snap["open_positions"], equity)
    return snap
