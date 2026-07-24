"""Risk layers status API — read-only view of the 12 protection layers."""
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db
from risk_layers import evaluate_layers
from route_utils import parse_object_id

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/layers")
async def risk_layers(account_id: str | None = None,
                      user=Depends(get_current_user)):
    db = get_db()
    account = None
    if account_id:
        account = await db.accounts.find_one(
            {"_id": parse_object_id(account_id)})
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        if (str(account.get("user_id")) != user["id"]
                and user.get("role") != "admin"):
            raise HTTPException(status_code=403, detail="Not your account")
    else:
        account = await db.accounts.find_one(
            {"user_id": user["id"], "status": {"$ne": "deleted"},
             "dormant": {"$ne": True}, "last_heartbeat": {"$ne": None}},
            sort=[("last_heartbeat", -1)])
    layers = await evaluate_layers(db, user["id"], account)
    return {"account_id": str(account["_id"]) if account else None,
            "account_label": (account or {}).get("label"),
            "layers": layers,
            "tripped": sum(1 for l in layers if l["status"] == "tripped"),
            "degraded": sum(1 for l in layers
                            if l["status"] in ("degraded", "error"))}


@router.get("/budget")
async def risk_budget_status(account_id: str | None = None,
                             user=Depends(get_current_user)):
    """Adaptive daily risk budget — per-strategy pool, spend and remaining."""
    db = get_db()
    if account_id:
        account = await db.accounts.find_one(
            {"_id": parse_object_id(account_id)})
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        if (str(account.get("user_id")) != user["id"]
                and user.get("role") != "admin"):
            raise HTTPException(status_code=403, detail="Not your account")
    cfg = await db.bot_configs.find_one(
        {"user_id": user["id"],
         **({"account_id": account_id} if account_id else
            {"$or": [{"account_id": None},
                     {"account_id": {"$exists": False}}]})}) or {}
    from risk_budget import budget_status
    return await budget_status(db, user["id"], cfg, account_id)


@router.get("/strategy-portfolio")
async def strategy_portfolio_report(account_id: str | None = None,
                                    user=Depends(get_current_user)):
    """Phase 3 — per-strategy metric table (expected return, volatility,
    drawdown, correlation, capacity, confidence) + dynamic allocation."""
    db = get_db()
    from risk_budget import DEFAULT_ALLOCATIONS
    from strategy_portfolio import build_allocation, strategy_metrics
    m = await strategy_metrics(db, user["id"], account_id)
    total = sum(v["n_trades"] for v in m["strategies"].values())
    dynamic = build_allocation(m["strategies"]) if total >= 10 else None
    return {**m,
            "total_trades": total,
            "base_allocation": DEFAULT_ALLOCATIONS,
            "dynamic_allocation": dynamic,
            "allocation_basis": "dynamic" if dynamic else
            "static (needs ≥10 closed bot trades in window)"}
