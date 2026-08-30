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
    # review P0-4 — position truth must NEVER read FULL while the caller's
    # own enabled accounts carry STALE/UNKNOWN/CONFLICTED positions.
    try:
        from state_contract import contract as state_contract
        from trading_authority import worst as worst_level
        sc = await state_contract(db, user["id"])
        rows = [r for r in sc["accounts"] if r.get("account_enabled")]
        bad = [r for r in rows if r["position_truth"] != "FRESH"]
        if bad:
            worst_truth = max((r["position_truth"] for r in bad),
                              key=lambda t: {"STALE": 1, "UNKNOWN": 2,
                                             "CONFLICTED": 3}.get(t, 1))
            reason = (f"{len(bad)} enabled account(s) with {worst_truth} "
                      "position truth — broker count is not authoritative")
            domains = out.setdefault("domains", {})
            domains["position_truth"] = {
                "level": worst_truth, "enforce_level": "CLOSE_ONLY",
                "reason": reason}
            out["level"] = worst_level(
                out.get("level") or "FULL", "CLOSE_ONLY")
            out["restricted"] = True
            out.setdefault("reasons", []).append(reason)
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
