"""iter-138/139 · Institutional quant routes (Phase B).

  POST /api/quant/bayes/run              — GP-EI parameter optimization run
  GET  /api/quant/bayes/proposals        — stored tuning proposals
  GET  /api/quant/allocator              — RL capital-allocator weights
  GET  /api/quant/portfolio-optimization — correlation/exposure report
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/quant", tags=["quant"])


class BayesRunRequest(BaseModel):
    engine: str
    symbol: str
    iters: int = 22


@router.post("/bayes/run")
async def bayes_run(req: BayesRunRequest, user=Depends(get_current_user)):
    from bayes_opt import run_bayes_optimization
    db = get_db()
    try:
        proposal = await run_bayes_optimization(
            db, user["id"], req.engine, req.symbol,
            iters=max(5, min(int(req.iters), 40)))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    proposal.pop("_id", None)
    return proposal


@router.get("/bayes/proposals")
async def bayes_proposals(engine: str | None = None,
                          user=Depends(get_current_user)):
    db = get_db()
    q = {"user_id": user["id"]}
    if engine:
        q["engine"] = engine
    docs = await db.tuning_proposals.find(q).sort("created_at", -1).to_list(50)
    for d in docs:
        d["_id"] = str(d["_id"])
    return {"proposals": docs}


@router.get("/allocator")
async def allocator_weights(user=Depends(get_current_user)):
    from rl_allocator import LOOKBACK_DAYS, MIN_TRADES, MIN_WEIGHT, get_allocations
    db = get_db()
    allocs = await get_allocations(db, user["id"])
    cfg = await db.bot_configs.find_one(
        {"user_id": user["id"], "active": True}, {"rl_allocator_mode": 1}) or {}
    return {
        "mode": str(cfg.get("rl_allocator_mode") or "advisory"),
        "params": {"lookback_days": LOOKBACK_DAYS, "min_trades": MIN_TRADES,
                   "min_weight": MIN_WEIGHT},
        "allocations": sorted(allocs.values(), key=lambda a: a["weight"]),
    }


class AllocatorModeRequest(BaseModel):
    mode: str


@router.post("/allocator/mode")
async def set_allocator_mode(req: AllocatorModeRequest,
                             user=Depends(get_current_user)):
    mode = str(req.mode or "").lower()
    if mode not in ("off", "advisory", "enforce"):
        raise HTTPException(status_code=422,
                            detail="mode must be off, advisory or enforce")
    db = get_db()
    res = await db.bot_configs.update_many(
        {"user_id": user["id"], "active": True},
        {"$set": {"rl_allocator_mode": mode}})
    return {"mode": mode, "configs_updated": res.modified_count}


@router.get("/portfolio-optimization")
async def portfolio_optimization(user=Depends(get_current_user)):
    from portfolio.optimizer import optimization_report
    db = get_db()
    return await optimization_report(db, user["id"])
