"""Global Trading Authority API — /api/authority"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from database import get_db
from trading_authority import (LEVELS, compute_authority, level_severity,
                               set_platform_level)

logger = logging.getLogger("trading.authority.api")

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
        # audit P0-1 — one decision: what is shown is what is enforced
        out["enforced_level"] = out["level"]
    except Exception as e:  # noqa: BLE001 — audit P1-1: typed UNKNOWN, never
        # the optimistic pre-merge verdict.
        import uuid
        corr = f"authfail_{uuid.uuid4().hex[:12]}"
        logger.error("authority merge failed for user %s [%s]: %s",
                     user.get("id"), corr, e)
        from trading_authority import worst as _worst
        out["authority_state"] = "UNKNOWN"
        out["correlation_id"] = corr
        out["level"] = _worst(out.get("level") or "FULL", "CLOSE_ONLY")
        out["enforced_level"] = _worst(out.get("enforced_level") or "FULL",
                                       "CLOSE_ONLY")
        out["restricted"] = True
        out.setdefault("reasons", []).append(
            "canonical readiness could not be merged — authority UNKNOWN, "
            f"new trades refused until it can be confirmed [{corr}]")
        return out
    out["authority_state"] = "KNOWN"
    from canonical_decision import decide_user
    out["decision"] = await decide_user(db, user["id"])
    out["level"] = worst_level(out["level"], {"READY": "FULL", "DEGRADED": "REDUCED", "CLOSE_ONLY": "CLOSE_ONLY",
                                              "BLOCKED": "LOCKED", "EMERGENCY": "EMERGENCY"}[out["decision"]["state"]])
    out["enforced_level"] = out["level"]
    out["restricted"] = out["level"] != "FULL"
    return out


@router.get("/decision")
async def decision_ep(user=Depends(get_current_user)):
    """Round 9 P0-01 — THE canonical decision every surface consumes."""
    from canonical_decision import decide_user
    return await decide_user(get_db(), user["id"])


@router.get("/decision/{account_id}")
async def account_decision_ep(account_id: str, user=Depends(get_current_user)):
    from bson import ObjectId
    from bson.errors import InvalidId
    from canonical_decision import decide_account
    db = get_db()
    try:
        q = {"_id": ObjectId(account_id)}
    except InvalidId:
        raise HTTPException(status_code=404, detail="account not found")
    if user.get("role") != "admin":
        q["user_id"] = user["id"]
    acc = await db.accounts.find_one(q)
    if not acc:
        raise HTTPException(status_code=404, detail="account not found")
    return await decide_account(db, acc)


@router.get("/inventory")
async def inventory_ep(user=Depends(get_current_user)):
    """Round 9 P0-02 — canonical inventory projection (admin: platform scope; user: own accounts)."""
    from inventory_projection import projection
    db = get_db()
    if user.get("role") == "admin":
        exp = await db.platform_state.find_one({"_id": "inventory_expectation"}) or {}
        return await projection(db, exp.get("scope_user_id"))
    return await projection(db, user["id"])


@router.post("/inventory/expectation")
async def inventory_expectation_ep(payload: dict, request: Request, user=Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    """Round 10 P1-01: PROPOSE the desired 6/3/3 state (step-up). A second admin approves."""
    from inventory_projection import propose_expectation
    for k in ("accounts", "enabled", "bots"):
        if not isinstance(payload.get(k), int):
            raise HTTPException(status_code=400, detail=f"{k} (int) required")
    db = get_db()
    from step_up import require_step_up, audit_event
    await require_step_up(db, user, request, "authority_relax")
    await audit_event(db, user["id"], "inventory_expectation_proposed", payload, request)
    return await propose_expectation(db, payload, user.get("email", ""))


@router.post("/inventory/expectation/approve")
async def inventory_expectation_approve_ep(request: Request, user=Depends(get_current_user)):
    """Second-admin approval of the pending expectation (step-up, proposer ≠ approver)."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    db = get_db()
    from step_up import require_step_up
    await require_step_up(db, user, request, "authority_relax")
    from inventory_projection import approve_expectation
    return await approve_expectation(db, user.get("email", ""))


@router.post("/inventory/approve")
async def inventory_approve_ep(payload: dict, request: Request, user=Depends(get_current_user)):
    """PROPOSE the CURRENT inventory hash as the configured state (step-up MFA).
    Round 12 P2-05: a DIFFERENT step-up admin must confirm (/inventory/approve/confirm)."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    db = get_db()
    from step_up import require_step_up
    await require_step_up(db, user, request, "authority_relax")
    from inventory_projection import approve_current
    exp = await db.platform_state.find_one({"_id": "inventory_expectation"}) or {}
    return await approve_current(db, user.get("email", ""), str(payload.get("note") or ""), exp.get("scope_user_id"))


@router.post("/inventory/approve/confirm")
async def inventory_approve_confirm_ep(request: Request, user=Depends(get_current_user)):
    """Second-admin confirmation of the pending inventory-hash proposal (hash + ids recomputed before commit)."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    db = get_db()
    from step_up import require_step_up
    await require_step_up(db, user, request, "authority_relax")
    from inventory_projection import confirm_current
    return await confirm_current(db, user.get("email", ""))


@router.get("/inventory/pending")
async def inventory_pending_ep(user=Depends(get_current_user)):
    """Admin go-live view: expectation (current + pending), hash approval pending, orphan bot configs."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    from inventory_projection import pending_hash_approval
    db = get_db()
    exp = await db.platform_state.find_one({"_id": "inventory_expectation"}, {"_id": 0}) or {}
    exp_pending = await db.platform_state.find_one({"_id": "inventory_expectation_pending"}, {"_id": 0}) or None
    pend = await pending_hash_approval(db)
    if pend:
        pend = {k: v for k, v in pend.items() if k != "_id"}
    orphan_q = {"$or": [{"account_id": None}, {"account_id": ""}, {"account_id": {"$exists": False}}]}
    orphans = []
    async for b in db.bot_configs.find(orphan_q, {"user_id": 1, "active": 1, "symbol": 1, "created_at": 1, "name": 1}).limit(50):
        orphans.append({"id": str(b["_id"]), "user_id": b.get("user_id"), "active": bool(b.get("active")),
                        "symbol": b.get("symbol"), "name": b.get("name"),
                        "created_at": b["created_at"].isoformat() if hasattr(b.get("created_at"), "isoformat") else b.get("created_at")})
    admins = await db.users.count_documents({"role": "admin"})
    from inventory_projection import approval_mode
    return {"expectation": exp, "expectation_pending": exp_pending, "hash_pending": pend,
            "orphan_bots": orphans, "orphan_total": await db.bot_configs.count_documents(orphan_q),
            "admin_count": admins, "me": user.get("email", ""), "approval_mode": approval_mode()}


@router.delete("/inventory/orphan-bots/{bot_id}")
async def inventory_delete_orphan_bot_ep(bot_id: str, request: Request, user=Depends(get_current_user)):
    """Delete ONE bot config that has no account id (inventory defect). Step-up + audited.
    Refuses to touch a bot that is bound to an account — those are deleted via the account flow."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    db = get_db()
    from bson import ObjectId
    from step_up import require_step_up, audit_event
    try:
        q = {"_id": ObjectId(bot_id)}
    except Exception:  # noqa: BLE001
        q = {"_id": bot_id}
    bot = await db.bot_configs.find_one(q)
    if not bot:
        raise HTTPException(status_code=404, detail="bot config not found")
    if bot.get("account_id"):
        raise HTTPException(status_code=409, detail={"code": "bot_has_account", "account_id": str(bot["account_id"])})
    await require_step_up(db, user, request, "authority_relax")
    await db.bot_configs.delete_one(q)
    await audit_event(db, user["id"], "inventory_orphan_bot_deleted",
                      {"bot_id": bot_id, "owner_user_id": bot.get("user_id"), "symbol": bot.get("symbol")}, request)
    return {"ok": True, "deleted": bot_id}


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
