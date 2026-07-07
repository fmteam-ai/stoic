"""iter-61 · Offline RL policy API."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/rl", tags=["rl"])


def _summary(policy: dict) -> dict:
    states = policy.get("states") or {}
    rows = [{"state": k, **v} for k, v in states.items()]
    rows.sort(key=lambda r: r["mean"] * (1 if r["n"] else 0))
    negative = [r for r in rows if r["n"] >= (policy.get("params") or {}).get("min_visits", 8)
                and r["mean"] < 0]
    return {
        "trained_at": policy.get("trained_at"),
        "trades_used": policy.get("trades_used"),
        "states_learned": len(states),
        "actionable_negative_states": len(negative),
        "params": policy.get("params"),
        "worst_states": rows[:8],
        "best_states": sorted(rows, key=lambda r: -r["mean"])[:5],
    }


@router.get("/policy")
async def get_rl_policy(user=Depends(get_current_user)):
    from rl_policy import get_policy
    db = get_db()
    uid = str(user.get("id") or user.get("_id"))
    return _summary(await get_policy(db, uid))


@router.post("/train")
async def retrain_rl_policy(user=Depends(get_current_user)):
    from rl_policy import train_policy
    db = get_db()
    uid = str(user.get("id") or user.get("_id"))
    return _summary(await train_policy(db, uid))
