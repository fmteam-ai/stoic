"""PAMM REST API — /api/pamm/*"""
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from database import get_db
from modules.pamm.permissions import (is_admin, require_admin,
                                      require_manager,
                                      require_program_access)

router = APIRouter(prefix="/pamm", tags=["pamm"])


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
async def create_program_ep(payload: dict, user=Depends(get_current_user)):
    db = get_db()
    require_admin(user)
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
        raise HTTPException(status_code=400, detail=str(e))  # deliberate ValueError message


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
    from services.broker_gateway.manager_api import pause_program
    return await pause_program(db, program, user["id"],
                               str((payload or {}).get("reason") or ""))


@router.post("/programs/{program_id}/resume")
async def resume_ep(program_id: str, user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    if program.get("emergency_stop"):
        raise HTTPException(status_code=409,
                            detail="Emergency stop engaged — admin must clear it")
    from services.broker_gateway.manager_api import resume_program
    return await resume_program(db, program, user["id"])


@router.post("/programs/{program_id}/emergency-stop")
async def emergency_stop_ep(program_id: str, payload: dict,
                            user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    from modules.pamm.risk import emergency_stop
    return await emergency_stop(db, program, user["id"],
                                str(payload.get("reason") or "manual"))


@router.post("/programs/{program_id}/clear-emergency-stop")
async def clear_estop_ep(program_id: str, user=Depends(get_current_user)):
    db = get_db()
    require_admin(user)
    await _program_or_404(db, program_id)
    await db.pamm_programs.update_one({"program_id": program_id},
                                      {"$set": {"emergency_stop": False}})
    return {"program_id": program_id, "emergency_stop": False}


@router.post("/programs/{program_id}/investors")
async def add_investor_ep(program_id: str, payload: dict,
                          user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
    try:
        amount = float(payload.get("amount") or 0)
        from modules.pamm.services import add_investor
        return await add_investor(
            db, program, {"name": payload.get("name"),
                          "email": payload.get("email")}, amount)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))  # deliberate ValueError message


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
async def reconcile_ep(program_id: str, user=Depends(get_current_user)):
    db = get_db()
    program = await _program_or_404(db, program_id)
    await require_program_access(db, user, program)
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
async def set_manager_ep(payload: dict, user=Depends(get_current_user)):
    """Admin-assigned manager role: {user_id, grant: bool}."""
    db = get_db()
    require_admin(user)
    from bson import ObjectId
    try:
        oid = ObjectId(str(payload.get("user_id") or ""))
    except Exception:
        raise HTTPException(status_code=400, detail="valid user_id required")
    r = await db.users.update_one(
        {"_id": oid}, {"$set": {"pamm_manager": bool(payload.get("grant"))}})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"user_id": str(oid), "pamm_manager": bool(payload.get("grant"))}


@router.post("/webhooks/{partner_id}")
async def webhook_ep(partner_id: str, request: Request):
    """Broker → STOIC signed webhooks (HMAC + replay window + idempotency)."""
    db = get_db()
    body = await request.body()
    from services.broker_gateway.webhook_handler import handle_webhook
    try:
        return await handle_webhook(
            db, partner_id,
            {k.lower(): v for k, v in request.headers.items()}, body)
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))  # deliberate ValueError message
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))  # deliberate ValueError message
