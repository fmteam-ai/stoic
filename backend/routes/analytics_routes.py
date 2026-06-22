"""Analytics routes — performance attribution endpoints."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from analytics import compute_attribution
from auto_tune import get_all_thresholds, get_auto_threshold, invalidate_cache
from learned_meta import retrain as learned_retrain, get_artifact as learned_artifact
from database import get_db

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/attribution")
async def get_attribution(user=Depends(get_current_user)):
    """Full performance attribution across every dimension."""
    return await compute_attribution(user["id"])


@router.get("/auto-tune")
async def get_auto_tune(user=Depends(get_current_user)):
    """Per-symbol auto-tuned min-confidence thresholds derived from closed trades."""
    db = get_db()
    cfg = await db.bot_configs.find_one({"user_id": user["id"]}) or {}
    risk = cfg.get("risk_level", "medium")
    symbols = cfg.get("symbols") or ["XAUUSD", "BTCUSD"]
    rows = await get_all_thresholds(user["id"], risk, symbols)
    return {
        "risk_level": risk,
        "symbols": symbols,
        "enabled": bool(cfg.get("auto_tune_enabled", True)),
        "thresholds": rows,
    }


@router.post("/auto-tune/refresh")
async def refresh_auto_tune(user=Depends(get_current_user)):
    """Invalidate cached thresholds — recomputed on next read."""
    invalidate_cache(user["id"])
    db = get_db()
    cfg = await db.bot_configs.find_one({"user_id": user["id"]}) or {}
    risk = cfg.get("risk_level", "medium")
    symbols = cfg.get("symbols") or ["XAUUSD", "BTCUSD"]
    rows = []
    for s in symbols:
        rows.append(await get_auto_threshold(user["id"], s, risk))
    return {"refreshed": True, "thresholds": rows}


@router.post("/learned-meta/retrain")
async def retrain_learned_meta(user=Depends(get_current_user)):
    """Retrain the local logistic-regression classifier on the latest closed
    trades. Returns the new artifact summary (or a reason if training was
    skipped due to insufficient data)."""
    res = await learned_retrain()
    return res


@router.get("/learned-meta")
async def get_learned_meta(user=Depends(get_current_user)):
    """Inspect the currently-active learned classifier."""
    art = await learned_artifact()
    if not art:
        return {"trained": False}
    # Don't expose mu/sd vectors — keep the response compact
    out = {k: art[k] for k in (
        "n_samples", "n_wins", "train_auc", "threshold",
        "trained_at", "feature_names",
    ) if k in art}
    out["trained"] = True
    return out
