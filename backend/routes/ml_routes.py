"""iter-108 · Stacked ML Ensemble API."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/ml", tags=["ml"])


def _summary(meta: dict) -> dict:
    return {"status": meta.get("status"),
            "trained_at": meta.get("trained_at"),
            "n_trades": meta.get("n_trades"),
            "aucs": meta.get("aucs"),
            "weights": meta.get("weights"),
            "models_saved": meta.get("models_saved"),
            "min_trades_required": 40}


@router.get("/ensemble")
async def get_ensemble_status(user=Depends(get_current_user)):
    from ml_ensemble import get_meta
    db = get_db()
    uid = str(user.get("id") or user.get("_id"))
    return _summary(await get_meta(db, uid))


@router.post("/train")
async def retrain_ensemble(user=Depends(get_current_user)):
    from ml_ensemble import train_ensemble
    db = get_db()
    uid = str(user.get("id") or user.get("_id"))
    return _summary(await train_ensemble(db, uid))
