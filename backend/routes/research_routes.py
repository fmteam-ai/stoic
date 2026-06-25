"""Routes for the Self-Improving Research Agent.

  GET  /api/research/proposals              — list pending + recent proposals
  POST /api/research/run                    — manual trigger (admin/user)
  GET  /api/research/last-run               — last_run_at metadata for the dashboard
  POST /api/research/proposals/{id}/accept  — apply to bot config
  POST /api/research/proposals/{id}/dismiss — reject
"""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db
from research_agent.self_improver import run_for_user
from route_utils import parse_object_id

router = APIRouter(prefix="/research", tags=["research-agent"])


def _serialise_proposal(d: dict) -> dict:
    return {
        "id": str(d.get("_id")),
        "name": d.get("name"),
        "rationale": d.get("rationale"),
        "compiled": d.get("compiled") or {},
        "backtest_summary": d.get("backtest_summary") or {},
        "score": d.get("score"),
        "delta_vs_baseline": d.get("delta_vs_baseline"),
        "beats_baseline": d.get("beats_baseline"),
        "status": d.get("status"),
        "created_at": d.get("created_at"),
        "accepted_at": d.get("accepted_at"),
        "dismissed_at": d.get("dismissed_at"),
    }


@router.get("/auto-accept")
async def get_auto_accept(user=Depends(get_current_user)):
    """Read the user's auto-accept settings (opt-in toggle + delta threshold)."""
    db = get_db()
    doc = await db.users.find_one({"_id": user["id"]}) or \
          await db.users.find_one({"id": user["id"]}) or {}
    s = doc.get("research_auto_accept") or {}
    return {
        "enabled": bool(s.get("enabled")),
        "min_delta_pct": float(s.get("min_delta_pct") or 10.0),
    }


@router.post("/auto-accept")
async def set_auto_accept(payload: dict, user=Depends(get_current_user)):
    """Update auto-accept settings. Body: {enabled: bool, min_delta_pct: float}."""
    enabled = bool(payload.get("enabled"))
    try:
        min_delta = float(payload.get("min_delta_pct") or 10.0)
    except Exception:
        raise HTTPException(status_code=400, detail="min_delta_pct must be numeric")
    if min_delta < 1 or min_delta > 100:
        raise HTTPException(status_code=400, detail="min_delta_pct must be 1-100")
    db = get_db()
    await db.users.update_one(
        {"_id": user["id"]},
        {"$set": {"research_auto_accept": {
            "enabled": enabled, "min_delta_pct": min_delta,
        }}},
        upsert=False,
    )
    return {"enabled": enabled, "min_delta_pct": min_delta}


@router.get("/proposals")
async def list_proposals(user=Depends(get_current_user)):
    db = get_db()
    cursor = db.improvement_proposals.find({
        "user_id": user["id"],
    }).sort("created_at", -1).limit(50)
    docs = await cursor.to_list(length=50)
    pending = [p for p in docs if p.get("status") == "pending"]
    history = [p for p in docs if p.get("status") != "pending"]
    return {
        "pending": [_serialise_proposal(p) for p in pending],
        "history": [_serialise_proposal(p) for p in history[:20]],
    }


@router.get("/last-run")
async def last_run(user=Depends(get_current_user)):
    db = get_db()
    doc = await db.research_runs.find_one({"user_id": user["id"]})
    if not doc:
        return {"last_run_at": None, "status": "never"}
    return {
        "last_run_at": doc.get("last_run_at"),
        "status": doc.get("last_status"),
        "trade_count": doc.get("last_trade_count"),
        "proposal_count": doc.get("last_proposal_count"),
        "baseline_score": doc.get("last_baseline_score"),
    }


@router.post("/run")
async def manual_run(user=Depends(get_current_user)):
    """Force-run the self-improvement loop right now (bypasses cooldown)."""
    db = get_db()
    result = await run_for_user(db, user["id"], force=True)
    # Trim heavy fields from the response — UI fetches proposals separately.
    return {
        "status": result.get("status"),
        "weaknesses": result.get("weaknesses"),
        "current_strategy": result.get("current_strategy"),
        "baseline": result.get("baseline"),
        "proposal_count": len(result.get("proposals") or []),
        "hypothesis_notes": result.get("hypothesis_notes") or [],
    }


@router.post("/proposals/{proposal_id}/accept")
async def accept_proposal(proposal_id: str, user=Depends(get_current_user)):
    db = get_db()
    oid = parse_object_id(proposal_id, "Proposal")
    doc = await db.improvement_proposals.find_one({
        "_id": oid, "user_id": user["id"],
    })
    if not doc:
        raise HTTPException(status_code=404, detail="proposal not found")
    if doc.get("status") != "pending":
        raise HTTPException(status_code=400, detail=f"proposal already {doc.get('status')}")

    compiled = doc.get("compiled") or {}
    now_iso = datetime.now(timezone.utc).isoformat()

    # Apply to bot_config — same fields nl_routes /strategy/apply uses
    update = {
        "symbols": compiled.get("symbols"),
        "session_preference": compiled.get("session_preference"),
        "risk_level": compiled.get("risk_level"),
        "strategy_style": compiled.get("strategy_style"),
        "max_concurrent_trades": compiled.get("max_concurrent_trades"),
        "updated_at": now_iso,
        "last_research_proposal_id": str(oid),
    }
    update = {k: v for k, v in update.items() if v is not None}
    await db.bot_configs.update_one(
        {"user_id": user["id"]},
        {"$set": update},
        upsert=False,
    )
    await db.improvement_proposals.update_one(
        {"_id": oid},
        {"$set": {"status": "accepted", "accepted_at": now_iso}},
    )
    return {"ok": True, "applied": update}


@router.post("/proposals/{proposal_id}/dismiss")
async def dismiss_proposal(proposal_id: str, user=Depends(get_current_user)):
    db = get_db()
    oid = parse_object_id(proposal_id, "Proposal")
    res = await db.improvement_proposals.update_one(
        {"_id": oid, "user_id": user["id"], "status": "pending"},
        {"$set": {"status": "dismissed",
                  "dismissed_at": datetime.now(timezone.utc).isoformat()}},
    )
    if res.modified_count == 0:
        raise HTTPException(status_code=404, detail="proposal not found or already actioned")
    return {"ok": True}
