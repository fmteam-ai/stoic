"""Analytics routes — performance attribution endpoints."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from analytics import compute_attribution, compute_sessions
from account_analytics import per_account_stats
from auto_tune import get_all_thresholds, get_auto_threshold, invalidate_cache
from learned_meta import retrain as learned_retrain, get_artifact as learned_artifact
from database import get_db

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/attribution")
async def get_attribution(user=Depends(get_current_user)):
    """Full performance attribution across every dimension."""
    return await compute_attribution(user["id"])


@router.get("/sessions")
async def get_sessions(user=Depends(get_current_user)):
    """Per-session breakdown — Asia / London / Overlap / NY / Off-hours.

    Returns win-rate, avg-R, expectancy-R and P&L per UTC session window so
    the trader can see which session their edge actually lives in.
    """
    return await compute_sessions(user["id"])


@router.post("/sessions/suggest-action")
async def suggest_session_action(user=Depends(get_current_user)):
    """Inspect the session breakdown and propose a single config tweak the
    user can apply with one click. Returns:
        {
          "action":  "tighten_worst" | "no_action",
          "session": "<bucket key>",
          "field":   "min_confidence_override",
          "from":    int, "to": int,
          "rationale": "<plain english>",
        }
    Strategy: if at least one bucket has ≥5 trades AND a win-rate below 40%,
    suggest tightening min_confidence by +5 for that session (clamped 95).
    """
    from database import get_db
    sessions = await compute_sessions(user["id"])
    buckets = sessions.get("buckets") or []
    worst = None
    for b in buckets:
        if b["count"] < 5:
            continue
        if b["win_rate"] >= 40:
            continue
        if worst is None or b["win_rate"] < worst["win_rate"]:
            worst = b
    if not worst:
        return {"action": "no_action",
                "rationale": "All sessions either trade above 40% win-rate or have fewer than 5 sampled trades."}

    db = get_db()
    cfg = await db.bot_configs.find_one({"user_id": user["id"], "account_id": None})
    cur_min = int((cfg or {}).get("min_confidence_override") or 65)
    new_min = min(95, cur_min + 5)
    return {
        "action": "tighten_worst",
        "session": worst["key"],
        "field": "min_confidence_override",
        "from": cur_min,
        "to": new_min,
        "rationale": (f"{worst['key']} has {worst['count']} trades at {worst['win_rate']}% win-rate "
                      f"(avg-R {worst['avg_r']}, P&L ${worst['total_pnl']}). "
                      f"Raising min_confidence to {new_min} will reduce signal volume "
                      f"in this session and require stronger conviction to fire."),
    }


@router.post("/sessions/apply-action")
async def apply_session_action(payload: dict, user=Depends(get_current_user)):
    """Apply the suggested config tweak returned by `suggest-action`. Body:
        {field: "min_confidence_override", to: 70}
    Only writes the default bot_config (account_id=None). Per-account
    overrides remain editable via the existing Bot Config page.
    """
    from database import get_db
    field = payload.get("field")
    if field != "min_confidence_override":
        raise HTTPException(status_code=400, detail="Only min_confidence_override is supported")
    try:
        new_val = int(payload.get("to"))
    except Exception:
        raise HTTPException(status_code=400, detail="`to` must be an integer")
    new_val = max(50, min(95, new_val))
    db = get_db()
    res = await db.bot_configs.update_one(
        {"user_id": user["id"], "account_id": None},
        {"$set": {"min_confidence_override": new_val,
                  "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    if res.matched_count == 0:
        # Create the default config if it doesn't exist yet
        await db.bot_configs.insert_one({
            "user_id": user["id"],
            "account_id": None,
            "min_confidence_override": new_val,
            "active": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    return {"ok": True, "field": field, "to": new_val}


@router.get("/by-account")
async def get_by_account(user=Depends(get_current_user)):
    """Side-by-side aggregates per MT5 account — feeds the dashboard's
    Per-Account Comparison widget (winner highlighting, 30d P&L, win rate, etc).
    """
    return await per_account_stats(user["id"])


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
