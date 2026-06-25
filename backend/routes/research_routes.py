"""Routes for the Self-Improving Research Agent.

  GET  /api/research/proposals              — list pending + recent proposals
  POST /api/research/run                    — manual trigger (admin/user)
  GET  /api/research/last-run               — last_run_at metadata for the dashboard
  GET  /api/research/proposals/{id}/targets — list candidate bots + which match this proposal
  POST /api/research/proposals/{id}/accept  — apply to bot config(s) — body: {target: "matching"|"all"|"default"|"<account_id>"}
  POST /api/research/proposals/{id}/dismiss — reject
"""
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from database import get_db
from research_agent.self_improver import run_for_user
from research_agent.proposal_targeting import (
    list_candidate_bots, resolve_target_configs, apply_proposal_to_configs,
)
from route_utils import parse_object_id

router = APIRouter(prefix="/research", tags=["research-agent"])


class AcceptBody(BaseModel):
    target: Optional[str] = "matching"


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


@router.get("/proposals/{proposal_id}/targets")
async def list_proposal_targets(proposal_id: str, user=Depends(get_current_user)):
    """List the user's bot_configs annotated with whether each one matches
    the proposal's symbol scope. Powers the target-selector dropdown.
    """
    db = get_db()
    oid = parse_object_id(proposal_id, "Proposal")
    doc = await db.improvement_proposals.find_one(
        {"_id": oid, "user_id": user["id"]},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="proposal not found")
    proposal_symbols = (doc.get("compiled") or {}).get("symbols") or []
    candidates = await list_candidate_bots(db, user["id"], proposal_symbols)
    matching_count = sum(1 for c in candidates if c["matches_proposal_symbols"])
    return {
        "proposal_symbols": proposal_symbols,
        "candidates": candidates,
        "matching_count": matching_count,
        "total_count": len(candidates),
    }


@router.post("/proposals/{proposal_id}/accept")
async def accept_proposal(proposal_id: str, body: AcceptBody | None = None,
                          user=Depends(get_current_user)):
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
    proposal_symbols = compiled.get("symbols") or []
    target = (body.target if body else None) or "matching"

    # Resolve which bot_configs to update.
    configs, resolved_mode = await resolve_target_configs(
        db, user["id"], proposal_symbols, target,
    )
    if not configs:
        raise HTTPException(
            status_code=422,
            detail=(
                f"No matching bot configs for target='{target}'. "
                "Try target='all' or pick a specific bot."
            ),
        )

    update_fields = {k: v for k, v in {
        "symbols": compiled.get("symbols"),
        "session_preference": compiled.get("session_preference"),
        "risk_level": compiled.get("risk_level"),
        "strategy_style": compiled.get("strategy_style"),
        "max_concurrent_trades": compiled.get("max_concurrent_trades"),
    }.items() if v is not None}

    audit = await apply_proposal_to_configs(
        db, configs,
        update_fields=update_fields,
        proposal_id=str(oid),
        target_mode=resolved_mode,
        auto=False,
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    await db.improvement_proposals.update_one(
        {"_id": oid},
        {"$set": {
            "status": "accepted",
            "accepted_at": now_iso,
            "applied_target_mode": resolved_mode,
            "applied_to_count": len(audit),
            "applied_audit": audit,
        }},
    )
    return {
        "ok": True,
        "applied_count": len(audit),
        "target_mode": resolved_mode,
        "applied_to": audit,
        "applied_fields": update_fields,
    }


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
