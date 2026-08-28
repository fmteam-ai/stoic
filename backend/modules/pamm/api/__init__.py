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
    from modules.pamm.risk import op_state_of, set_op_state
    from modules.pamm.risk.states import severity
    if severity(op_state_of(program)) < severity("new_trades_paused"):
        return await set_op_state(
            db, program, "new_trades_paused", user["id"],
            reason=str((payload or {}).get("reason") or ""), source="human")
    from services.broker_gateway.manager_api import pause_program
    return await pause_program(db, program, user["id"],
                               str((payload or {}).get("reason") or ""))


@router.post("/programs/{program_id}/resume")
async def resume_ep(program_id: str, request: Request,
                    user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    from modules.pamm.risk import op_state_of, set_op_state
    from modules.pamm.risk.states import severity
    if program.get("emergency_stop") or severity(
            op_state_of(program)) > severity("new_trades_paused"):
        raise HTTPException(
            status_code=409,
            detail="Emergency state engaged — de-escalate via op-state "
                   "(admin) first")
    await _rl(db, request, user["id"], "pamm_mutate")
    # resume RE-ENABLES trading → risk-increasing → fresh MFA required
    await _step_up(db, user, request, "pamm_resume",
                   {"program_id": program_id})
    try:
        return await set_op_state(db, program, "running", user["id"],
                                  source="human", allow_deescalate=True)
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))


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
    program = await _program_or_404(db, program_id)
    await _rl(db, request, user["id"], "pamm_mutate")
    # clearing an e-stop re-arms trading → fresh MFA required
    await _step_up(db, user, request, "pamm_clear_emergency_stop",
                   {"program_id": program_id})
    from modules.pamm.risk import op_state_of, set_op_state
    from modules.pamm.risk.states import severity
    try:
        if severity(op_state_of(program)) > severity("new_trades_paused"):
            await set_op_state(db, program, "new_trades_paused", user["id"],
                               reason="clear emergency stop",
                               source="human", allow_deescalate=True)
        else:
            await db.pamm_programs.update_one(
                {"program_id": program_id},
                {"$set": {"emergency_stop": False}})
    except PermissionError as e:  # LOCKED → dual authorization only
        raise HTTPException(status_code=409, detail=str(e))
    return {"program_id": program_id, "emergency_stop": False,
            "op_state": "new_trades_paused"}


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
    # LOOSENING any protection needs a SECOND admin (dual authorization)
    from modules.pamm.dualauth import create_change_request, is_loosening
    loosened = is_loosening(merged, clean)
    if loosened:
        try:
            req = await create_change_request(
                db, program, "risk_limits_increase",
                {"risk_limits_patch": clean}, user["id"],
                reason=f"loosens: {', '.join(loosened)}")
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"program_id": program_id, "pending_approval": True,
                "change_id": req["change_id"], "loosens": loosened,
                "message": "This change weakens protection — a SECOND "
                           "admin must approve it before it takes effect."}
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


@router.post("/programs/{program_id}/op-state")
async def op_state_ep(program_id: str, payload: dict, request: Request,
                      user=Depends(get_current_user)):
    """Move a program along the emergency hierarchy. Escalation = instant;
    de-escalation = human + step-up MFA; leaving LOCKED = dual auth only."""
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    await _rl(db, request, user["id"], "pamm_mutate")
    from modules.pamm.risk import op_state_of, set_op_state
    from modules.pamm.risk.states import OP_STATES, severity
    target = str(payload.get("state") or "")
    if target not in OP_STATES:
        raise HTTPException(status_code=400,
                            detail=f"state must be one of {OP_STATES}")
    if severity(target) < severity(op_state_of(program)):
        await _step_up(db, user, request, "pamm_op_state_deescalate",
                       {"program_id": program_id, "to": target})
    try:
        return await set_op_state(db, program, target, user["id"],
                                  reason=str(payload.get("reason") or ""),
                                  source="human", allow_deescalate=True)
    except PermissionError as e:  # LOCKED → dual authorization only
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/programs/{program_id}/trade-verdict")
async def trade_verdict_ep(program_id: str, payload: dict,
                           user=Depends(get_current_user)):
    """APPROVE / REDUCE / REJECT a candidate trade's risk request."""
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    from modules.pamm.risk import trade_verdict
    try:
        v = await trade_verdict(
            db, program, float(payload.get("requested_risk_pct") or 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail="requested_risk_pct must be positive")
    if v.get("verdict") in ("REDUCE", "REJECT"):
        try:
            from verdict_tracking import record_verdict
            v["verdict_tracking_id"] = await record_verdict(
                db, source="pamm_verdict", verdict=v["verdict"],
                requested=v["requested_risk_pct"],
                approved=v["approved_risk_pct"], unit="risk_pct",
                user_id=user["id"], program_id=program_id,
                limiting_factor=v.get("limiting_factor"),
                factors=v.get("factors"),
                reasons=[v.get("primary_reason") or v.get("reason") or ""],
                nav=v.get("nav"),
                context={"symbol": payload.get("symbol"),
                         "side": payload.get("side"),
                         "entry_price": payload.get("entry_price"),
                         "stop_loss": payload.get("stop_loss"),
                         "take_profit": payload.get("take_profit")})
        except Exception:
            pass
    return v


@router.get("/programs/{program_id}/change-requests")
async def change_requests_ep(program_id: str,
                             user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    from modules.pamm.dualauth import list_change_requests
    return {"requests": await list_change_requests(db, program_id)}


@router.post("/programs/{program_id}/change-requests")
async def create_change_ep(program_id: str, payload: dict, request: Request,
                           user=Depends(get_current_user)):
    db = get_db()
    require_admin(user)
    program = await _program_or_404(db, program_id)
    await _rl(db, request, user["id"], "pamm_mutate")
    await _step_up(db, user, request, "pamm_change_request",
                   {"program_id": program_id,
                    "kind": payload.get("kind")})
    from modules.pamm.dualauth import create_change_request
    try:
        return await create_change_request(
            db, program, str(payload.get("kind") or ""),
            dict(payload.get("payload") or {}), user["id"],
            reason=str(payload.get("reason") or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/change-requests/{change_id}/{decision}")
async def decide_change_ep(change_id: str, decision: str, request: Request,
                           user=Depends(get_current_user)):
    if decision not in ("approve", "reject"):
        raise HTTPException(status_code=404, detail="Not found")
    db = get_db()
    require_admin(user)
    await _rl(db, request, user["id"], "pamm_mutate")
    await _step_up(db, user, request, "pamm_change_decide",
                   {"change_id": change_id, "decision": decision})
    from modules.pamm.dualauth import decide_change_request
    try:
        return await decide_change_request(db, change_id,
                                           decision == "approve", user["id"])
    except PermissionError as e:  # same-admin approval attempt
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/partners/{partner_id}/certify")
async def certify_partner_ep(partner_id: str, request: Request,
                             user=Depends(get_current_user)):
    """Run the standard adapter certification contract (v54 §14)."""
    db = get_db()
    require_admin(user)
    partner = await db.broker_partners.find_one(
        {"partner_id": partner_id}, {"_id": 0})
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")
    await _rl(db, request, user["id"], "pamm_health", max_attempts=10)
    from services.broker_gateway.certification import certify_adapter
    return await certify_adapter(db, partner)


@router.get("/incidents")
async def incidents_ep(status: str | None = None,
                       user=Depends(get_current_user)):
    db = get_db()
    await require_manager(db, user)
    q = {"status": status} if status in ("open", "resolved") else {}
    return {"incidents": [i async for i in db.pamm_incidents.find(
        q, {"_id": 0}).sort("opened_at", -1).limit(100)]}


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


@router.get("/partners")
async def partners_ep(user=Depends(get_current_user)):
    db = get_db()
    await require_manager(db, user)
    from services.broker_gateway.pamm_api import redact_partner
    return {"partners": [redact_partner(p) async for p in
                         db.broker_partners.find({}, {"_id": 0})
                         .sort("created_at", 1)]}


@router.post("/partners")
async def create_partner_ep(payload: dict, request: Request,
                            user=Depends(get_current_user)):
    """Register a REAL broker partner (rest / mt5_manager) — admin +
    step-up MFA; credentials are vault-encrypted at rest."""
    db = get_db()
    require_admin(user)
    await _rl(db, request, user["id"], "pamm_mutate")
    await _step_up(db, user, request, "pamm_partner_create",
                   {"name": payload.get("name"),
                    "adapter": payload.get("adapter")})
    from services.broker_gateway.pamm_api import register_partner
    try:
        return await register_partner(db, payload, user["id"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/programs/{program_id}/position-truth")
async def position_truth_ep(program_id: str,
                            user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    expected = await db.pamm_expected_positions.find_one(
        {"program_id": program_id}, {"_id": 0})
    history = [h async for h in db.pamm_position_truth.find(
        {"program_id": program_id}, {"_id": 0}).sort("at", -1).limit(20)]
    incident = await db.pamm_incidents.find_one(
        {"program_id": program_id, "type": "position_drift",
         "status": "open"}, {"_id": 0})
    return {"truth": program.get("position_truth"),
            "drift_tolerance": float(program.get("drift_tolerance") or 0.0),
            "expected": expected, "history": history,
            "open_incident": incident}


@router.post("/programs/{program_id}/position-truth/check")
async def position_truth_check_ep(program_id: str, request: Request,
                                  user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    await _rl(db, request, user["id"], "pamm_health")
    from modules.pamm.reconciliation.position_truth import \
        check_position_truth
    try:
        return await check_position_truth(db, program, actor=user["id"])
    except Exception as e:
        logger.warning("position truth check failed on %s: %s",
                       program_id, e)
        raise HTTPException(status_code=502,
                            detail="Broker positions unavailable")


@router.post("/programs/{program_id}/position-truth/acknowledge")
async def position_truth_ack_ep(program_id: str, request: Request,
                                user=Depends(get_current_user)):
    """Adopt broker truth as expected state. Trading REMAINS frozen —
    resume is a separate step-up-gated action."""
    db = get_db()
    require_admin(user)
    program = await _program_or_404(db, program_id)
    await _rl(db, request, user["id"], "pamm_mutate")
    from step_up import audit_event
    await audit_event(db, user["id"], "pamm_position_truth_ack",
                      {"program_id": program_id}, request)
    from modules.pamm.reconciliation.position_truth import acknowledge_drift
    return await acknowledge_drift(db, program, user["id"])


@router.put("/programs/{program_id}/drift-tolerance")
async def drift_tolerance_ep(program_id: str, payload: dict,
                             request: Request,
                             user=Depends(get_current_user)):
    db = get_db()
    require_admin(user)
    program = await _program_or_404(db, program_id)
    await _rl(db, request, user["id"], "pamm_mutate")
    from modules.pamm.reconciliation.position_truth import \
        set_drift_tolerance
    try:
        return await set_drift_tolerance(
            db, program, float(payload.get("tolerance") or 0), user["id"])
    except PermissionError as e:  # increases go through dual auth
        raise HTTPException(status_code=409, detail=str(e))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail="tolerance must be a number >= 0")


# ═══════════════ v62.1 — PAMM Strategy Profiles ═══════════════════════════

@router.get("/strategies")
async def pamm_strategies_ep(user=Depends(get_current_user)):
    """PAMM-eligible strategies from the central registry + versions
    + feature flags + risk profiles."""
    db = get_db()
    await require_manager(db, user)
    from modules.pamm.risk_profiles import list_profiles
    from modules.pamm.strategy_assignment import feature_flags
    from strategies.registry import pamm_eligible_strategies, strategy_hash
    return {"strategies": [
                {**d.model_dump(), "strategy_hash": strategy_hash(d)}
                for d in pamm_eligible_strategies()],
            "modes": {"SINGLE": True, "MULTI": False, "DYNAMIC_AI": False},
            "feature_flags": feature_flags(),
            "risk_profiles": await list_profiles(db)}


@router.get("/strategies/nitro-eligibility")
async def nitro_eligibility_ep(account_id: str | None = None,
                               user=Depends(get_current_user)):
    db = get_db()
    await require_manager(db, user)
    acc = None
    if account_id:
        from route_utils import parse_object_id
        q = {"_id": parse_object_id(account_id)}
        if not is_admin(user):
            q["user_id"] = user["id"]
        acc = await db.accounts.find_one(q)
        if not acc:
            raise HTTPException(status_code=404, detail="Account not found")
    from strategies.nitro.eligibility import eligibility
    return await eligibility(db, user["id"], acc)


@router.get("/programs/{program_id}/strategy")
async def get_program_strategy_ep(program_id: str,
                                  user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    from modules.pamm.strategy_assignment import get_assignment
    return await get_assignment(db, program_id)


@router.post("/programs/{program_id}/strategy")
async def assign_program_strategy_ep(program_id: str, payload: dict,
                                     request: Request,
                                     user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_manager(db, user)
    await require_program_access(db, user, program)
    await _rl(db, request, user["id"], "pamm_mutate")
    from modules.pamm.strategy_assignment import assign
    out = await assign(db, program, payload, user["id"])
    if out.get("error"):
        code = 409 if out["error"] == "assignment_exists" else 400
        raise HTTPException(status_code=code, detail=out)
    return out


@router.patch("/programs/{program_id}/strategy")
async def patch_program_strategy_ep(program_id: str, payload: dict,
                                    request: Request,
                                    user=Depends(get_current_user)):
    """Material changes (risk profile / re-enable) are risk-affecting →
    step-up MFA, and they invalidate the previous validation +
    certification (REVALIDATION_REQUIRED)."""
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_manager(db, user)
    await require_program_access(db, user, program)
    if "risk_profile_id" in payload or payload.get("enabled") is True:
        await _step_up(db, user, request, "pamm_strategy_patch",
                       {"program_id": program_id,
                        "fields": sorted(payload)})
    else:
        await _rl(db, request, user["id"], "pamm_mutate")
    from modules.pamm.strategy_assignment import patch
    out = await patch(db, program, payload, user["id"])
    if out.get("error"):
        raise HTTPException(status_code=400, detail=out)
    return out


@router.post("/programs/{program_id}/strategy/validate")
async def validate_program_strategy_ep(program_id: str, request: Request,
                                       user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_manager(db, user)
    await require_program_access(db, user, program)
    await _rl(db, request, user["id"], "pamm_mutate")
    from modules.pamm.strategy_assignment import validate
    out = await validate(db, program, user["id"])
    if out.get("error"):
        raise HTTPException(status_code=400, detail=out)
    return out


@router.post("/programs/{program_id}/strategy/activate")
async def activate_program_strategy_ep(program_id: str, request: Request,
                                       user=Depends(get_current_user)):
    """Live activation is risk-affecting → step-up MFA required."""
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_manager(db, user)
    await require_program_access(db, user, program)
    await _step_up(db, user, request, "pamm_strategy_activate",
                   {"program_id": program_id})
    from modules.pamm.strategy_assignment import activate
    out = await activate(db, program, user["id"])
    if out.get("error"):
        code = 403 if out["error"] == "nitro_live_disabled" else 409
        raise HTTPException(status_code=code, detail=out)
    return out


@router.post("/programs/{program_id}/strategy/suspend")
async def suspend_program_strategy_ep(program_id: str, payload: dict,
                                      request: Request,
                                      user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_manager(db, user)
    await require_program_access(db, user, program)
    await _rl(db, request, user["id"], "pamm_mutate")
    from modules.pamm.strategy_assignment import suspend
    out = await suspend(db, program, user["id"],
                        str(payload.get("reason") or ""))
    if out.get("error"):
        raise HTTPException(status_code=409, detail=out)
    return out


@router.post("/programs/{program_id}/strategy/change")
async def change_program_strategy_ep(program_id: str, payload: dict,
                                     request: Request,
                                     user=Depends(get_current_user)):
    """Guarded change: requires flat program + step-up MFA. Positions are
    NEVER inherited by the new strategy."""
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_manager(db, user)
    await require_program_access(db, user, program)
    await _step_up(db, user, request, "pamm_strategy_change",
                   {"program_id": program_id,
                    "new_strategy": payload.get("strategy_id")})
    from modules.pamm.strategy_assignment import change
    out = await change(db, program, payload, user["id"])
    if out.get("error"):
        code = 409 if out["error"] in ("program_not_flat",
                                       "assignment_exists") else 400
        raise HTTPException(status_code=code, detail=out)
    return out


# ═══════ v62.2 — PAMM × Strategy Certification Campaigns ══════════════════

@router.get("/programs/{program_id}/strategy-ownership")
async def strategy_ownership_ep(program_id: str,
                                user=Depends(get_current_user)):
    """Position Truth strategy ownership — open positions that cannot be
    attributed to the active strategy assignment are FLAGGED
    (PAMM_OWNER_UNKNOWN / STRATEGY_OWNER_UNKNOWN /
    STRATEGY_VERSION_MISMATCH), never guessed."""
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    from modules.pamm.reconciliation.strategy_ownership import \
        strategy_ownership
    return await strategy_ownership(db, program)


@router.get("/programs/{program_id}/certification")
async def cert_campaign_status_ep(program_id: str,
                                  user=Depends(get_current_user)):
    """Campaign state + stage criteria + live evaluation preview."""
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    from strategies.certification_campaign import status
    return await status(db, program)


@router.get("/programs/{program_id}/certification/evidence")
async def cert_campaign_evidence_ep(program_id: str,
                                    user=Depends(get_current_user)):
    """Hash-chained evidence records for the current campaign."""
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    from strategies.certification_campaign import evidence, get_campaign
    camp = await get_campaign(db, program_id)
    if not camp:
        raise HTTPException(status_code=404, detail="No campaign")
    return await evidence(db, camp["campaign_id"])


@router.post("/programs/{program_id}/certification/start")
async def cert_campaign_start_ep(program_id: str, request: Request,
                                 user=Depends(get_current_user)):
    db = get_db()
    require_admin(user)
    program = await _program_or_404(db, program_id)
    await _rl(db, request, user["id"], "pamm_mutate")
    from strategies.certification_campaign import start
    out = await start(db, program, user["id"])
    if out.get("error"):
        code = 409 if out["error"] == "campaign_exists" else 400
        raise HTTPException(status_code=code, detail=out)
    return out


@router.post("/programs/{program_id}/certification/checkpoint")
async def cert_campaign_checkpoint_ep(program_id: str, payload: dict,
                                      request: Request,
                                      user=Depends(get_current_user)):
    """Admin-recorded, audited, hash-chained stage evidence checkpoint."""
    db = get_db()
    require_admin(user)
    program = await _program_or_404(db, program_id)
    await _rl(db, request, user["id"], "pamm_mutate")
    from strategies.certification_campaign import checkpoint
    out = await checkpoint(db, program, payload, user["id"])
    if out.get("error"):
        raise HTTPException(status_code=400, detail=out)
    return out


@router.post("/programs/{program_id}/certification/evaluate")
async def cert_campaign_evaluate_ep(program_id: str, request: Request,
                                    user=Depends(get_current_user)):
    """Run the current stage-gate evaluation against merged auto +
    checkpoint metrics and record it in the evidence chain."""
    db = get_db()
    require_admin(user)
    program = await _program_or_404(db, program_id)
    await _rl(db, request, user["id"], "pamm_mutate")
    from strategies.certification_campaign import evaluate
    out = await evaluate(db, program, user["id"])
    if out.get("error"):
        raise HTTPException(status_code=409, detail=out)
    return out


@router.post("/programs/{program_id}/certification/advance")
async def cert_campaign_advance_ep(program_id: str, request: Request,
                                   user=Depends(get_current_user)):
    """Advance the campaign one lifecycle stage — impossible without a
    PASSING evaluation of the current stage. Admin + step-up MFA."""
    db = get_db()
    require_admin(user)
    program = await _program_or_404(db, program_id)
    await _step_up(db, user, request, "pamm_cert_advance",
                   {"program_id": program_id})
    from strategies.certification_campaign import advance
    out = await advance(db, program, user["id"])
    if out.get("error"):
        raise HTTPException(status_code=409, detail=out)
    return out


@router.post("/programs/{program_id}/certification/revoke")
async def cert_campaign_revoke_ep(program_id: str, payload: dict,
                                  request: Request,
                                  user=Depends(get_current_user)):
    """Abort a mid-pipeline campaign (→ DRAFT) or revoke an issued
    certification (→ REVOKED, cert revoked, assignment flagged)."""
    db = get_db()
    require_admin(user)
    program = await _program_or_404(db, program_id)
    await _step_up(db, user, request, "pamm_cert_revoke",
                   {"program_id": program_id,
                    "reason": payload.get("reason")})
    from strategies.certification_campaign import revoke_campaign
    out = await revoke_campaign(db, program, user["id"],
                                str(payload.get("reason") or "manual"))
    if out.get("error"):
        raise HTTPException(status_code=409, detail=out)
    return out
