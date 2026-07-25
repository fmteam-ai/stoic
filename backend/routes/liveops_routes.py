"""Phase 3/4 · Live-ops API — capital stage, operator actions, real-time
risk composite, subsystem self-monitoring."""
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db

router = APIRouter(tags=["live-ops"])


@router.get("/risk/capital-stage")
async def get_capital_stage(user=Depends(get_current_user)):
    from capital_stages import capital_stage
    return await capital_stage(get_db(), user["id"])


@router.get("/subsystems/health")
async def get_subsystem_health(user=Depends(get_current_user)):
    from subsystem_health import subsystem_conservatism
    return await subsystem_conservatism(get_db(), user["id"])


@router.post("/operator/action")
async def operator_action(payload: dict, user=Depends(get_current_user)):
    from operator_actions import ACTIONS, run_action
    action = str(payload.get("action") or "")
    if action not in ACTIONS:
        raise HTTPException(status_code=400,
                            detail=f"action must be one of {sorted(ACTIONS)}")
    try:
        return await run_action(get_db(), user["id"], action,
                                payload.get("params") or {})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))  # ValueError: crafted validation text


@router.get("/operator/actions")
async def list_operator_actions(user=Depends(get_current_user)):
    from operator_actions import ACTIONS
    return {"actions": [{"action": k, "description": v}
                        for k, v in ACTIONS.items()]}


@router.get("/modes/guardian")
async def mode_guardian(user=Depends(get_current_user)):
    """Correction #6 — auto-demotion ladder status + recovery gate."""
    from auto_demotion import GREEN_HOURS, recovery_status
    db = get_db()
    rec = await recovery_status(db, user["id"])
    demotions = []
    async for d in db.mode_demotions.find(
            {"user_id": user["id"]}).sort("at", -1).limit(10):
        demotions.append({"at": str(d.get("at")),
                          "reason": d.get("reason"),
                          "ceiling": d.get("ceiling"),
                          "health_overall": d.get("health_overall"),
                          "demotions": d.get("demotions") or []})
    return {"recovery": rec, "recent_demotions": demotions,
            "green_hours_required": GREEN_HOURS}


@router.get("/risk/realtime")
async def risk_realtime(user=Depends(get_current_user)):
    """Phase 3.3 — one composite for the operational screen."""
    db = get_db()
    accounts = []
    async for a in db.accounts.find({"user_id": user["id"],
                                     "status": {"$ne": "deleted"}}):
        accounts.append({"id": str(a["_id"]),
                         "label": a.get("label") or a.get("broker"),
                         "equity": a.get("equity"),
                         "balance": a.get("balance"),
                         "currency": a.get("base_currency")})
    open_by_symbol: dict = {}
    open_by_scope: dict = {}
    open_n = 0
    async for t in db.trades.find({"user_id": user["id"], "status": "open"},
                                  {"symbol": 1, "scope": 1, "lot_size": 1}):
        open_n += 1
        open_by_symbol[t.get("symbol")] = \
            open_by_symbol.get(t.get("symbol"), 0) + 1
        sc = t.get("scope") or "unattributed"
        open_by_scope[sc] = open_by_scope.get(sc, 0) + 1
    intel = {}
    async for s in db.broker_intel_scores.find(
            {"account_id": {"$in": [a["id"] for a in accounts]}}
            ).sort("at", -1).limit(20):
        acc = s.get("account_id")
        if acc not in intel:
            comps = s.get("components") or {}
            intel[acc] = {"score": s.get("score"),
                          "latency": comps.get("latency"),
                          "slippage": comps.get("slippage")}
    from shadow_health import health_score
    hs = await health_score(db, user["id"])
    from subsystem_health import subsystem_conservatism
    sub = await subsystem_conservatism(db, user["id"])
    from capital_stages import capital_stage
    stage = await capital_stage(db, user["id"])
    return {"accounts": accounts,
            "open_trades": {"total": open_n, "by_symbol": open_by_symbol,
                            "by_scope": open_by_scope},
            "broker_intel": intel,
            "shadow_health": {"overall": hs.get("overall"),
                              "components": hs.get("components"),
                              "promotions_paused":
                                  hs.get("promotions_paused")},
            "subsystems": sub, "capital_stage": stage}
