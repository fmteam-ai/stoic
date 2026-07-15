"""Scalp subsystem API — config, status, decisions, metrics, retrain."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from database import get_db
from route_utils import parse_object_id

router = APIRouter(prefix="/scalp", tags=["scalp"])


class ScalpConfigRequest(BaseModel):
    account_id: str
    symbol: str = "EURUSD"
    enabled: bool = False
    mode: str = "shadow"          # shadow | demo_live
    confirm_live: bool = False


@router.post("/config")
async def set_config(req: ScalpConfigRequest, user=Depends(get_current_user)):
    from scalp.instruments import approved
    if approved(req.symbol) is None:
        raise HTTPException(status_code=422,
                            detail=f"{req.symbol} is not in the approved scalp universe (EURUSD only)")
    if req.mode not in ("shadow", "demo_live"):
        raise HTTPException(status_code=422, detail="mode must be shadow or demo_live")
    if req.mode == "demo_live" and not req.confirm_live:
        raise HTTPException(status_code=422,
                            detail="demo_live requires confirm_live=true — orders WILL be sent to the broker")
    db = get_db()
    account = await db.accounts.find_one({"_id": parse_object_id(req.account_id, "Account"),
                                          "user_id": user["id"]})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    from scalp.engine import apply_config
    runner = await apply_config(db, account, req.symbol, req.enabled, req.mode)
    return {"ok": True, "status": runner.status()}


@router.get("/status")
async def status(account_id: str = None, user=Depends(get_current_user)):
    db = get_db()
    from scalp.engine import _runners, get_runner
    # hydrate runners from persisted configs so status survives restarts
    q = {"user_id": user["id"]}
    if account_id:
        q["account_id"] = account_id
    async for cfg in db.scalp_configs.find(q):
        r = get_runner(cfg["account_id"], cfg["user_id"], cfg["symbol"])
        if r is not None and not r.enabled and cfg.get("enabled"):
            r.enabled = True
            r.mode = cfg.get("mode", "shadow")
    out = [r.status() for r in _runners.values() if r.user_id == user["id"]
           and (not account_id or r.account_id == account_id)]
    return {"runners": out}


@router.get("/decisions")
async def decisions(limit: int = 50, symbol: str = None,
                    user=Depends(get_current_user)):
    db = get_db()
    q = {"user_id": user["id"]}
    if symbol:
        q["symbol"] = symbol.upper()
    docs = await db.scalp_decisions.find(q).sort("ts_ms", -1).to_list(min(limit, 200))
    for d in docs:
        d["id"] = str(d.pop("_id"))
    return {"decisions": docs}


@router.get("/metrics")
async def metrics(symbol: str = "EURUSD", user=Depends(get_current_user)):
    """Step 18 production metrics: alpha / execution / risk / stability."""
    db = get_db()
    q = {"user_id": user["id"], "symbol": symbol.upper(),
         "outcome.result": {"$in": ["target_first", "stop_first", "timeout"]}}
    docs = await db.scalp_decisions.find(q).sort("ts_ms", -1).to_list(5000)
    n = len(docs)
    if n == 0:
        return {"symbol": symbol.upper(), "n": 0}
    tf = [d for d in docs if d["outcome"]["result"] == "target_first"]
    sf = [d for d in docs if d["outcome"]["result"] == "stop_first"]
    net = [float(d["outcome"].get("net_pips") or 0) for d in docs]
    costs = [float(d.get("cost_pips") or 0) for d in docs]
    wins = [p for p in net if p > 0]
    losses = [p for p in net if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    resolved_dir = len(tf) + len(sf)
    model_doc = await db.scalp_models.find_one({"symbol": symbol.upper()})
    tick_count = await db.scalp_ticks.count_documents({"user_id": user["id"],
                                                       "symbol": symbol.upper()})
    return {
        "symbol": symbol.upper(), "n": n,
        "alpha": {
            "target_before_stop_rate": round(len(tf) / resolved_dir, 3) if resolved_dir else None,
            "gross_expectancy_pips": round(sum(net) / n, 3),
            "net_expectancy_pips": round((sum(net) - sum(costs)) / n, 3),
            "avg_winner_pips": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loser_pips": round(sum(losses) / len(losses), 2) if losses else None,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "timeout_rate": round(sum(1 for d in docs if d["outcome"]["result"] == "timeout") / n, 3),
        },
        "execution": {
            "avg_cost_pips": round(sum(costs) / n, 2),
            "avg_spread_pips": round(sum(float(d["forecast"]["expected_spread_cost_pips"])
                                         for d in docs) / n, 2),
        },
        "model": ({"oos_auc": model_doc.get("oos_auc"), "n_samples": model_doc.get("n_samples"),
                   "usable": model_doc.get("usable"), "trained_at": model_doc.get("trained_at")}
                  if model_doc else None),
        "data": {"tick_batches_recorded": tick_count},
        "verdicts": {
            "shadow_traded": sum(1 for d in docs if d.get("verdict") == "shadow_traded"),
            "live_traded": sum(1 for d in docs if d.get("verdict") == "live_traded"),
            "rejected": sum(1 for d in docs if d.get("verdict") == "rejected"),
        },
    }


@router.post("/retrain")
async def retrain(symbol: str = "EURUSD", user=Depends(get_current_user)):
    from scalp.model import retrain as do_retrain
    return await do_retrain(get_db(), symbol.upper())
