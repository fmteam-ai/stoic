"""PAMM REST API — /api/pamm/*"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from database import get_db
from modules.pamm.permissions import (is_admin, require_admin,
                                      require_manager,
                                      require_program_access)

logger = logging.getLogger("pamm.api")

router = APIRouter(prefix="/pamm", tags=["pamm"])


async def _rl(db, request: Request, identifier: str, scope: str,
              max_attempts: int = 30) -> None:
    from security import rate_limit
    await rate_limit(db, scope, identifier, max_attempts, 60,
                     request=request)


async def _step_up(db, user, request: Request, action_name: str,
                   detail: dict) -> None:
    """Risk-increasing PAMM mutations require fresh step-up MFA (SEC-001)."""
    from step_up import audit_event, require_step_up
    await require_step_up(db, user, request, "risk_raise")
    await audit_event(db, user["id"], action_name, detail, request,
                      step_up=True)


async def _program_or_404(db, program_id: str) -> dict:
    from modules.pamm.services import get_program
    p = await get_program(db, program_id)
    if not p:
        raise HTTPException(status_code=404, detail="Program not found")
    return p


@router.get("/programs")
async def list_programs_ep(user=Depends(get_current_user)):
    db = get_db()
    await require_manager(db, user)
    from modules.pamm.services import list_programs
    mid = None if is_admin(user) else user["id"]
    return {"programs": await list_programs(db, mid)}


@router.post("/programs")
async def create_program_ep(payload: dict, request: Request,
                            user=Depends(get_current_user)):
    db = get_db()
    require_admin(user)
    await _rl(db, request, user["id"], "pamm_mutate")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    from modules.pamm.services import create_program
    try:
        return await create_program(
            db, name, str(payload.get("manager_id") or user["id"]),
            partner_id=str(payload.get("partner_id") or "prt_sandbox"),
            currency=str(payload.get("currency") or "USD"),
            manager_fee_pct=float(payload.get("manager_fee_pct") or 20.0))
    except ValueError as e:
        logger.warning("pamm create_program rejected: %s", e)
        raise HTTPException(
            status_code=400,
            detail="Program creation failed — verify the program exists "
                   "on the broker.")


@router.get("/programs/{program_id}")
async def program_detail(program_id: str, user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    from modules.pamm.reports import performance_summary
    from modules.pamm.risk import trading_allowed
    allowed, reason = await trading_allowed(db, program)
    return {"program": program,
            "performance": await performance_summary(db, program_id),
            "trading_allowed": allowed, "trading_block_reason": reason}


@router.post("/programs/{program_id}/pause")
async def pause_ep(program_id: str, request: Request,
                   user=Depends(get_current_user), payload: dict | None = None):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    await _rl(db, request, user["id"], "pamm_mutate")
    from services.broker_gateway.manager_api import pause_program
    return await pause_program(db, program, user["id"],
                               str((payload or {}).get("reason") or ""))


@router.post("/programs/{program_id}/resume")
async def resume_ep(program_id: str, request: Request,
                    user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    if program.get("emergency_stop"):
        raise HTTPException(status_code=409,
                            detail="Emergency stop engaged — admin must clear it")
    await _rl(db, request, user["id"], "pamm_mutate")
    # resume RE-ENABLES trading → risk-increasing → fresh MFA required
    await _step_up(db, user, request, "pamm_resume",
                   {"program_id": program_id})
    from services.broker_gateway.manager_api import resume_program
    return await resume_program(db, program, user["id"])


@router.post("/programs/{program_id}/emergency-stop")
async def emergency_stop_ep(program_id: str, payload: dict, request: Request,
                            user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    await _rl(db, request, user["id"], "pamm_mutate")
    from modules.pamm.risk import emergency_stop
    return await emergency_stop(db, program, user["id"],
                                str(payload.get("reason") or "manual"))


@router.post("/programs/{program_id}/clear-emergency-stop")
async def clear_estop_ep(program_id: str, request: Request,
                         user=Depends(get_current_user)):
    db = get_db()
    require_admin(user)
    await _program_or_404(db, program_id)
    await _rl(db, request, user["id"], "pamm_mutate")
    # clearing an e-stop re-arms trading → fresh MFA required
    await _step_up(db, user, request, "pamm_clear_emergency_stop",
                   {"program_id": program_id})
    await db.pamm_programs.update_one({"program_id": program_id},
                                      {"$set": {"emergency_stop": False}})
    return {"program_id": program_id, "emergency_stop": False}


@router.post("/programs/{program_id}/investors")
async def add_investor_ep(program_id: str, payload: dict, request: Request,
                          user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    await _rl(db, request, user["id"], "pamm_mutate")
    try:
        amount = float(payload.get("amount") or 0)
        from modules.pamm.services import add_investor
        return await add_investor(
            db, program, {"name": payload.get("name"),
                          "email": payload.get("email")}, amount)
    except ValueError as e:
        logger.warning("pamm add_investor rejected on %s: %s", program_id, e)
        raise HTTPException(
            status_code=400,
            detail="Allocation rejected — amount must be positive and the "
                   "investor valid on the broker.")


@router.get("/programs/{program_id}/allocations")
async def allocations_ep(program_id: str, user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    allocs = [a async for a in db.pamm_allocations.find(
        {"program_id": program_id}, {"_id": 0}).sort("at", -1).limit(200)]
    return {"allocations": allocs}


@router.get("/programs/{program_id}/nav")
async def nav_ep(program_id: str, user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    from modules.pamm.services import nav_history
    return {"nav": await nav_history(db, program_id)}


@router.get("/programs/{program_id}/master")
async def master_ep(program_id: str, user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    from services.broker_gateway.manager_api import master_account
    return await master_account(db, program)


@router.post("/programs/{program_id}/reconcile")
async def reconcile_ep(program_id: str, request: Request,
                       user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    await _rl(db, request, user["id"], "pamm_mutate")
    from services.broker_gateway.reconciliation import reconcile_program
    return await reconcile_program(db, program)


@router.get("/programs/{program_id}/reconciliation")
async def recon_history_ep(program_id: str, user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    docs = [d async for d in db.pamm_reconciliation.find(
        {"program_id": program_id}, {"_id": 0}).sort("at", -1).limit(50)]
    return {"reconciliation": docs}


@router.get("/events")
async def events_ep(program_id: str | None = None,
                    user=Depends(get_current_user)):
    db = get_db()
    await require_manager(db, user)
    from modules.pamm.events import recent_events
    return {"events": await recent_events(db, program_id)}


@router.post("/managers")
async def set_manager_ep(payload: dict, request: Request,
                         user=Depends(get_current_user)):
    """Admin-assigned manager role: {user_id, grant: bool}."""
    db = get_db()
    require_admin(user)
    await _rl(db, request, user["id"], "pamm_mutate")
    from bson import ObjectId
    try:
        oid = ObjectId(str(payload.get("user_id") or ""))
    except Exception:
        raise HTTPException(status_code=400, detail="valid user_id required")
    # privilege grant → fresh MFA required
    await _step_up(db, user, request, "pamm_manager_grant",
                   {"user_id": str(oid), "grant": bool(payload.get("grant"))})
    r = await db.users.update_one(
        {"_id": oid}, {"$set": {"pamm_manager": bool(payload.get("grant"))}})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"user_id": str(oid), "pamm_manager": bool(payload.get("grant"))}


@router.get("/programs/{program_id}/risk-limits")
async def get_risk_limits_ep(program_id: str,
                             user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    from modules.pamm.risk import get_limits
    return {"program_id": program_id, "risk_limits": get_limits(program),
            "risk_breach": program.get("risk_breach")}


@router.put("/programs/{program_id}/risk-limits")
async def put_risk_limits_ep(program_id: str, payload: dict, request: Request,
                             user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    await _rl(db, request, user["id"], "pamm_mutate")
    from modules.pamm.risk import get_limits, validate_limits_patch
    try:
        clean = validate_limits_patch(payload)
    except (ValueError, TypeError) as e:
        # input-validation feedback only (field names/ranges) — safe to echo
        raise HTTPException(status_code=400, detail=str(e))
    # editing safety caps is risk-increasing → fresh MFA required
    await _step_up(db, user, request, "pamm_risk_limits_update",
                   {"program_id": program_id, "patch": clean})
    merged = get_limits(program)
    for k, v in clean.items():
        merged[k].update(v)
    await db.pamm_programs.update_one(
        {"program_id": program_id}, {"$set": {"risk_limits": merged}})
    return {"program_id": program_id, "risk_limits": merged}


@router.get("/programs/{program_id}/risk-status")
async def risk_status_ep(program_id: str, user=Depends(get_current_user)):
    """Pure evaluation — no enforcement side-effects."""
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    from modules.pamm.risk import (evaluate_program, get_limits,
                                   news_blackout_status, trading_allowed)
    result = await evaluate_program(db, program)
    result["news"] = await news_blackout_status(
        db, get_limits(program)["news_filter"])
    allowed, reason = await trading_allowed(db, program)
    result["trading_allowed"] = allowed
    result["trading_block_reason"] = reason
    result["risk_breach"] = program.get("risk_breach")
    return result


@router.post("/programs/{program_id}/risk-check")
async def risk_check_ep(program_id: str, request: Request,
                        user=Depends(get_current_user)):
    """Evaluate AND enforce (halt/flatten on breach)."""
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    await _rl(db, request, user["id"], "pamm_mutate")
    from modules.pamm.risk import run_risk_check
    return await run_risk_check(db, program, actor=user["id"])


@router.post("/programs/{program_id}/clear-risk-breach")
async def clear_risk_breach_ep(program_id: str, request: Request,
                               user=Depends(get_current_user)):
    db = get_db()
    require_admin(user)
    await _program_or_404(db, program_id)
    await _rl(db, request, user["id"], "pamm_mutate")
    # clearing a breach re-arms trading → fresh MFA required
    await _step_up(db, user, request, "pamm_clear_risk_breach",
                   {"program_id": program_id})
    await db.pamm_programs.update_one(
        {"program_id": program_id}, {"$unset": {"risk_breach": ""}})
    return {"program_id": program_id, "risk_breach": None}


@router.get("/news")
async def news_ep(user=Depends(get_current_user)):
    db = get_db()
    await require_manager(db, user)
    from modules.pamm.risk import get_calendar
    cal = await get_calendar(db)
    high = [e for e in cal["events"] if e["impact"] == "high"]
    return {"source": cal["source"], "fetched_at": cal.get("fetched_at"),
            "error": cal.get("error"), "total_events": len(cal["events"]),
            "high_impact": high[:100]}


@router.get("/health")
async def broker_health_ep(user=Depends(get_current_user)):
    db = get_db()
    await require_manager(db, user)
    from services.broker_gateway.health import health_overview
    return {"partners": await health_overview(db)}


@router.post("/health/check")
async def broker_health_check_ep(request: Request,
                                 user=Depends(get_current_user)):
    db = get_db()
    await require_manager(db, user)
    await _rl(db, request, user["id"], "pamm_health", max_attempts=10)
    from services.broker_gateway.health import heartbeat_all
    return {"results": await heartbeat_all(db)}


@router.get("/marketplace")
async def marketplace_ep(user=Depends(get_current_user)):
    """Published program listings — any authenticated user may browse."""
    db = get_db()
    from modules.pamm.marketplace import public_listings
    return {"listings": await public_listings(db)}


@router.post("/marketplace/{program_id}/join")
async def join_request_ep(program_id: str, payload: dict, request: Request,
                          user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    if not (program.get("published") and program.get("status") == "active"):
        raise HTTPException(status_code=404, detail="Program not found")
    await _rl(db, request, user["id"], "pamm_join", max_attempts=5)
    from modules.pamm.marketplace import create_join_request
    try:
        amount = float(payload.get("amount") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="valid amount required")
    try:
        return await create_join_request(
            db, program, user, amount, str(payload.get("note") or ""))
    except ValueError as e:  # user-input validation feedback only
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/marketplace/my-requests")
async def my_requests_ep(user=Depends(get_current_user)):
    db = get_db()
    from modules.pamm.marketplace import my_requests
    return {"requests": await my_requests(db, user["id"])}


@router.post("/programs/{program_id}/publish")
async def publish_ep(program_id: str, payload: dict, request: Request,
                     user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    await _rl(db, request, user["id"], "pamm_mutate")
    from modules.pamm.marketplace import set_published
    return await set_published(db, program, bool(payload.get("publish")),
                               payload.get("pitch"))


@router.get("/programs/{program_id}/join-requests")
async def join_requests_ep(program_id: str, user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    from modules.pamm.marketplace import list_join_requests
    return {"requests": await list_join_requests(db, program_id)}


@router.post("/join-requests/{request_id}/{decision}")
async def decide_join_ep(request_id: str, decision: str, request: Request,
                         user=Depends(get_current_user)):
    if decision not in ("approve", "reject"):
        raise HTTPException(status_code=404, detail="Not found")
    db = get_db()
    req = await db.pamm_join_requests.find_one(
        {"request_id": request_id}, {"_id": 0})
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    program = await _program_or_404(db, req["program_id"])
    await require_program_access(db, user, program)
    await _rl(db, request, user["id"], "pamm_mutate")
    from modules.pamm.marketplace import decide_join_request
    try:
        return await decide_join_request(db, request_id,
                                         decision == "approve", user["id"])
    except ValueError as e:
        logger.warning("pamm join decision failed on %s: %s", request_id, e)
        raise HTTPException(status_code=409,
                            detail="Request already decided or unavailable")


@router.get("/sweep-status")
async def sweep_status_ep(user=Depends(get_current_user)):
    db = get_db()
    await require_manager(db, user)
    from modules.pamm.sweep import sweep_status
    return await sweep_status(db)


@router.post("/webhooks/{partner_id}")
async def webhook_ep(partner_id: str, request: Request):
    """Broker → STOIC signed webhooks (HMAC + replay window + idempotency)."""
    db = get_db()
    from security import rate_limit
    await rate_limit(db, "pamm_webhook", partner_id, 600, 60,
                     request=request)
    body = await request.body()
    from services.broker_gateway.webhook_handler import handle_webhook
    try:
        return await handle_webhook(
            db, partner_id,
            {k.lower(): v for k, v in request.headers.items()}, body)
    except PermissionError as e:
        logger.warning("pamm webhook auth failure for %s: %s", partner_id, e)
        raise HTTPException(status_code=401, detail="unauthorized")
    except ValueError as e:
        logger.warning("pamm webhook rejected for %s: %s", partner_id, e)
        raise HTTPException(status_code=400, detail="invalid webhook")
