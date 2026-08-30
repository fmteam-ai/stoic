"""Global Trading Authority API — /api/authority"""
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from database import get_db
from trading_authority import (LEVELS, compute_authority, level_severity,
                               set_platform_level)

router = APIRouter(prefix="/authority", tags=["authority"])


@router.get("")
async def authority_ep(user=Depends(get_current_user)):
    db = get_db()
    out = await compute_authority(db)
    # audit v4 P0-1 — the ribbon must derive from the SAME canonical
    # readiness object as every other surface: position truth can never
    # read FULL while readiness reports stale truth or pending
    # reconciliation for the caller's accounts.
    try:
        from state_contract import contract as _contract
        from trading_authority import worst as worst_level
        from trading_readiness import readiness as _readiness
        sc = await _contract(db, user["id"])
        rd = await _readiness(db, user["id"])
        codes = {r["code"]: r for r in rd.get("reasons", [])}
        # canonical worst-of truth — the SAME object the OPEN widget shows
        truth = sc.get("position_truth") or "UNKNOWN"
        pending = codes.get("RECONCILIATION_PENDING")
        if truth != "FRESH" or pending:
            level = truth if truth != "FRESH" else "STALE"
            reason = (pending["message"] if pending and truth == "FRESH"
                      else f"canonical position truth is {level} — broker "
                           "count cannot be confirmed"
                      + (f"; {pending['message']}" if pending else ""))
            domains = out.setdefault("domains", {})
            domains["position_truth"] = {
                "level": level, "enforce_level": "CLOSE_ONLY",
                "reason": reason}
            out["level"] = worst_level(
                out.get("level") or "FULL", "CLOSE_ONLY")
            out["restricted"] = True
            out.setdefault("reasons", []).append(reason)
        # the EXECUTION pill must never read FULL while readiness reports
        # enabled bots blocked from executing — mirror the readiness
        # reason so the ribbon and the readiness strip agree.
        exb = codes.get("EXECUTION_BLOCKED") or codes.get("PANIC_TRIPPED")
        if exb:
            dom = out.setdefault("domains", {})
            if (dom.get("execution") or {}).get("level") in (None, "FULL"):
                dom["execution"] = {"level": "REDUCED",
                                    "reason": exb["message"]}
                out["level"] = worst_level(
                    out.get("level") or "FULL", "REDUCED")
                out["restricted"] = True
                out.setdefault("reasons", []).append(exb["message"])
        out["readiness_level"] = rd.get("level")
    except Exception:  # noqa: BLE001 — display fallback, never 500 the strip
        pass
    return out


@router.post("/platform")
async def set_platform_ep(payload: dict, request: Request,
                          user=Depends(get_current_user)):
    """Set the platform-wide authority override. Restricting (safer) is a
    single admin action; RELAXING (riskier) additionally requires
    step-up MFA."""
    db = get_db()
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    level = str(payload.get("level") or "").upper()
    if level not in LEVELS:
        raise HTTPException(status_code=400,
                            detail=f"level must be one of {LEVELS}")
    prev = await db.platform_state.find_one({"_id": "trading_authority"})
    prev_level = (prev or {}).get("level") or "FULL"
    if level_severity(level) < level_severity(prev_level):
        from step_up import require_step_up
        await require_step_up(db, user, request, "authority_relax")
    from step_up import audit_event
    await audit_event(db, user["id"], "trading_authority_platform",
                      {"from": prev_level, "to": level,
                       "reason": payload.get("reason")}, request)
    return await set_platform_level(db, level,
                                    str(payload.get("reason") or ""),
                                    user["id"])
