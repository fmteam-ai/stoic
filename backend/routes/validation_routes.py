"""MT5 validation evidence ledger + staged live-deployment gate.

Phase D — every pre-live MT5 scenario needs recorded PASS evidence
(docs/MT5_VALIDATION_CAMPAIGN.md maps the procedures). Evidence lives in
`validation_evidence` (append-only; latest record per scenario+account_mode wins).

Phase E — deployment stage state machine with promotion criteria:
    internal_shadow → demo_broker → small_live → larger_live → production
Advisory by default; set STAGE_ENFORCEMENT=true to block live bot activation
below small_live.
"""
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from database import get_db
from routes.ops_routes import _ops_actor

router = APIRouter(tags=["validation"])

VALIDATION_SCENARIOS = (
    "restart_recovery",
    "reconnect_recovery",
    "duplicate_commands",
    "stale_acknowledgements",
    "partial_fills",
    "multi_deal_fills",
    "netting",
    "hedging",
    "rejected_orders",
    "emergency_close",
    "manual_broker_intervention",
    "long_running_broker_sync",
)
ACCOUNT_MODES = ("netting", "hedging")

STAGES = ("internal_shadow", "demo_broker", "small_live",
          "larger_live", "production")
# minimum days the CURRENT stage must have run before promotion
STAGE_MIN_DAYS = {"internal_shadow": 3, "demo_broker": 14,
                  "small_live": 14, "larger_live": 30}


class EvidenceIn(BaseModel):
    status: str = Field(pattern="^(pass|fail)$")
    account_mode: str = Field(pattern="^(netting|hedging)$")
    notes: str = Field(min_length=5, max_length=2000)
    evidence_ref: str | None = Field(default=None, max_length=500)


def _now_utc():
    return datetime.now(timezone.utc)


async def _latest_evidence(db):
    """{scenario: {mode: latest record}} — newest record per pair wins."""
    out = {s: {} for s in VALIDATION_SCENARIOS}
    async for e in db.validation_evidence.find({}).sort("recorded_at", 1):
        if e.get("scenario") in out:
            e["id"] = str(e.pop("_id"))
            out[e["scenario"]][e.get("account_mode")] = e
    return out


def _validation_complete(latest: dict) -> bool:
    return all(
        (latest[s].get(m) or {}).get("status") == "pass"
        for s in VALIDATION_SCENARIOS for m in ACCOUNT_MODES)


@router.get("/ops/validation")
async def validation_status(request: Request):
    allowed, _ = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    db = get_db()
    latest = await _latest_evidence(db)
    scenarios = []
    for s in VALIDATION_SCENARIOS:
        modes = {m: ({"status": rec["status"], "notes": rec["notes"],
                      "recorded_by": rec.get("recorded_by"),
                      "recorded_at": rec.get("recorded_at"),
                      "evidence_ref": rec.get("evidence_ref")}
                     if (rec := latest[s].get(m)) else None)
                 for m in ACCOUNT_MODES}
        scenarios.append({"scenario": s, "modes": modes,
                          "complete": all((v or {}).get("status") == "pass"
                                          for v in modes.values())})
    return {"scenarios": scenarios,
            "complete": _validation_complete(latest),
            "required_modes": list(ACCOUNT_MODES)}


@router.post("/ops/validation/{scenario}")
async def record_evidence(scenario: str, payload: EvidenceIn,
                          request: Request):
    allowed, actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    if scenario not in VALIDATION_SCENARIOS:
        return JSONResponse(status_code=404, content={
            "detail": f"unknown scenario — one of {list(VALIDATION_SCENARIOS)}"})
    db = get_db()
    doc = {"scenario": scenario, "account_mode": payload.account_mode,
           "status": payload.status, "notes": payload.notes,
           "evidence_ref": payload.evidence_ref,
           "recorded_by": actor, "recorded_at": _now_utc()}
    res = await db.validation_evidence.insert_one(doc)
    return {"ok": True, "id": str(res.inserted_id)}


async def _stage_doc(db):
    doc = await db.platform_state.find_one({"_id": "deployment_stage"})
    if not doc:
        doc = {"_id": "deployment_stage", "stage": STAGES[0],
               "entered_at": _now_utc(), "history": []}
        await db.platform_state.insert_one(doc)
    return doc


async def _promotion_criteria(db, doc):
    """Criteria the CURRENT stage must satisfy to promote to the next."""
    stage = doc["stage"]
    if stage == STAGES[-1]:
        return None, []
    next_stage = STAGES[STAGES.index(stage) + 1]
    criteria = []

    # 1 · minimum days in current stage
    min_days = STAGE_MIN_DAYS.get(stage, 0)
    try:
        entered = datetime.fromisoformat(str(doc.get("entered_at")))
        days_in = (datetime.now(timezone.utc) - entered).total_seconds() / 86400
    except Exception:
        days_in = 0
    criteria.append({"name": f"min_{min_days}_days_in_stage",
                     "ok": days_in >= min_days,
                     "detail": f"{days_in:.1f}/{min_days} days"})

    # 2 · no unacknowledged critical alerts
    crit = await db.ops_alerts.count_documents(
        {"acked_at": None, "severity": "critical"})
    criteria.append({"name": "no_unacked_critical_alerts",
                     "ok": crit == 0, "detail": f"{crit} open"})

    # 3 · MT5 validation campaign complete before ANY live stage
    if next_stage in ("small_live", "larger_live", "production"):
        latest = await _latest_evidence(db)
        done = sum(1 for s in VALIDATION_SCENARIOS for m in ACCOUNT_MODES
                   if (latest[s].get(m) or {}).get("status") == "pass")
        total = len(VALIDATION_SCENARIOS) * len(ACCOUNT_MODES)
        criteria.append({"name": "mt5_validation_campaign_complete",
                         "ok": done == total, "detail": f"{done}/{total} pass"})
    return next_stage, criteria


@router.get("/ops/stage")
async def get_stage(request: Request):
    allowed, _ = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    db = get_db()
    doc = await _stage_doc(db)
    next_stage, criteria = await _promotion_criteria(db, doc)
    return {"stage": doc["stage"], "entered_at": doc.get("entered_at"),
            "stages": list(STAGES), "next_stage": next_stage,
            "promotion_criteria": criteria,
            "can_promote": bool(next_stage) and all(c["ok"] for c in criteria),
            "enforcement": os.environ.get(
                "STAGE_ENFORCEMENT", "false").lower() == "true",
            "history": doc.get("history", [])[-10:]}


class StageChangeIn(BaseModel):
    force: bool = False
    reason: str | None = Field(default=None, max_length=500)


@router.post("/ops/stage/promote")
async def promote_stage(payload: StageChangeIn, request: Request):
    allowed, actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    db = get_db()
    doc = await _stage_doc(db)
    next_stage, criteria = await _promotion_criteria(db, doc)
    if not next_stage:
        return JSONResponse(status_code=409,
                            content={"detail": "already at production"})
    failed = [c for c in criteria if not c["ok"]]
    if failed and not payload.force:
        return JSONResponse(status_code=409, content={
            "detail": "promotion criteria not met",
            "failed": failed,
            "hint": "pass force=true with a reason to override (audited)"})
    if failed and payload.force and not (payload.reason or "").strip():
        return JSONResponse(status_code=422, content={
            "detail": "forced promotion requires a reason"})
    entry = {"from": doc["stage"], "to": next_stage, "by": actor,
             "at": _now_utc(), "forced": bool(failed),
             "reason": payload.reason}
    await db.platform_state.update_one(
        {"_id": "deployment_stage"},
        {"$set": {"stage": next_stage, "entered_at": _now_utc()},
         "$push": {"history": entry}})
    return {"ok": True, "stage": next_stage, "forced": bool(failed)}


@router.post("/ops/stage/demote")
async def demote_stage(payload: StageChangeIn, request: Request):
    allowed, actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    if not (payload.reason or "").strip():
        return JSONResponse(status_code=422,
                            content={"detail": "demotion requires a reason"})
    db = get_db()
    doc = await _stage_doc(db)
    idx = STAGES.index(doc["stage"])
    if idx == 0:
        return JSONResponse(status_code=409,
                            content={"detail": "already at the first stage"})
    prev_stage = STAGES[idx - 1]
    entry = {"from": doc["stage"], "to": prev_stage, "by": actor,
             "at": _now_utc(), "forced": False, "reason": payload.reason}
    await db.platform_state.update_one(
        {"_id": "deployment_stage"},
        {"$set": {"stage": prev_stage, "entered_at": _now_utc()},
         "$push": {"history": entry}})
    return {"ok": True, "stage": prev_stage}


async def live_stage_gate(db) -> str | None:
    """Enforcement hook — returns a block-reason when live activation is
    forbidden by the current deployment stage (STAGE_ENFORCEMENT=true)."""
    if os.environ.get("STAGE_ENFORCEMENT", "false").lower() != "true":
        return None
    doc = await _stage_doc(db)
    if STAGES.index(doc["stage"]) < STAGES.index("small_live"):
        return (f"Deployment stage is '{doc['stage']}' — live activation "
                f"requires stage 'small_live' or beyond. Promote via "
                f"/api/ops/stage/promote once criteria pass.")
    return None
