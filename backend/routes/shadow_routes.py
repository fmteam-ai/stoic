"""Shadow routes — paper-shadow signal track record (iter-62) + shadow
MODEL testing lab (iter-140, institutional Phase C).

  GET  /api/shadow/performance            — hypothetical shadow-signal record
  GET  /api/shadow/models                 — challengers (lazily re-evaluated)
  POST /api/shadow/models/register        — lock a challenger version
  POST /api/shadow/models/{id}/promote    — apply after the P3 gate passes
  POST /api/shadow/models/{id}/retire     — retire / roll back
  GET  /api/shadow/reconciliation         — simulator vs broker-fill honesty
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth import get_current_user
from database import get_db
from shadow_performance import compute_aggregate

router = APIRouter(prefix="/shadow", tags=["shadow"])


@router.get("/performance")
async def shadow_performance(since_days: int = Query(90, ge=1, le=365),
                             limit: int = Query(500, ge=1, le=2000),
                             user=Depends(get_current_user)):
    db = get_db()
    return await compute_aggregate(db, user["id"], since_days=since_days, limit=limit)


class RegisterModelRequest(BaseModel):
    proposal_id: str | None = None
    engine: str | None = None
    symbol: str | None = None
    params: dict | None = None
    note: str = ""


@router.post("/models/register")
async def register_shadow_model(req: RegisterModelRequest,
                                user=Depends(get_current_user)):
    from model_shadow import register_model
    db = get_db()
    engine, symbol, params, source = req.engine, req.symbol, req.params, "manual"
    if req.proposal_id:
        from bson import ObjectId
        try:
            prop = await db.tuning_proposals.find_one(
                {"_id": ObjectId(req.proposal_id), "user_id": user["id"]})
        except Exception:
            prop = None
        if not prop:
            raise HTTPException(status_code=404, detail="Proposal not found")
        engine, symbol, params = prop["engine"], prop["symbol"], prop["params"]
        source = "bayes_opt"
        await db.tuning_proposals.update_one(
            {"_id": prop["_id"]}, {"$set": {"status": "shadow_testing"}})
    if not engine or not symbol:
        raise HTTPException(status_code=422,
                            detail="Provide proposal_id OR engine+symbol+params")
    try:
        model = await register_model(db, user["id"], engine, symbol,
                                     params or {}, source=source, note=req.note)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return model


@router.get("/models")
async def list_shadow_models(user=Depends(get_current_user)):
    from model_shadow import evaluate_user_models, promotion_status
    db = get_db()
    testing = await evaluate_user_models(db, user["id"])
    finished = await db.shadow_models.find(
        {"user_id": user["id"], "status": {"$in": ["promoted", "retired"]}}
    ).sort("registered_at", -1).to_list(30)
    for m in finished:
        m["_id"] = str(m["_id"])
        m["promotion"] = promotion_status(m)
    return {"testing": testing, "finished": finished}


@router.post("/models/{model_id}/promote")
async def promote_shadow_model(model_id: str, user=Depends(get_current_user)):
    from model_shadow import promote_model
    db = get_db()
    try:
        return await promote_model(db, user["id"], model_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/models/{model_id}/retire")
async def retire_shadow_model(model_id: str, user=Depends(get_current_user)):
    from model_shadow import retire_model
    db = get_db()
    try:
        return await retire_model(db, user["id"], model_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/reconciliation")
async def fill_reconciliation(days: int = Query(14, ge=1, le=90),
                              user=Depends(get_current_user)):
    from model_shadow import reconcile_fills
    db = get_db()
    return await reconcile_fills(db, user["id"], days=days)
