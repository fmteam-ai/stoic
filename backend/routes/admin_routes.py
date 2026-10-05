"""Admin moderation — manage users and affiliates.

Endpoints (all require role=admin except GET /api/terms which is public):

  Public
    GET    /api/terms                                  Terms of Use (md + version)

  Admin · Users
    GET    /api/admin/users                            list users (filter ?status=)
    GET    /api/admin/users/{user_id}                  user detail + recent audit
    POST   /api/admin/users/{user_id}/suspend          {reason}  → status=suspended
    POST   /api/admin/users/{user_id}/unsuspend        restores status=active
    POST   /api/admin/users/{user_id}/terminate        {reason}  → status=terminated
    POST   /api/admin/users/{user_id}/restore          terminated → active (rare)

  Admin · Affiliates
    GET    /api/admin/affiliates                       list (filter ?status=)
    POST   /api/admin/affiliates/{aff_id}/suspend      {reason}  → active=false
    POST   /api/admin/affiliates/{aff_id}/unsuspend    re-activates
    POST   /api/admin/affiliates/{aff_id}/terminate    {reason}  → terminated=true,
                                                       forfeits unpaid balance

  Admin · Audit
    GET    /api/admin/audit-log?limit=200              recent moderation actions

Side effects on suspend/terminate user:
  - users.status = "suspended" | "terminated"
  - users.suspension_reason / terminated_reason recorded
  - all of the user's bot_configs are flipped `active=false` (bot stops trading)
  - if the user is an affiliate, their affiliate row is also flagged
  - audit row inserted into `admin_audit_log`

Login flow consults users.status — see auth_routes.login.
"""
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from database import get_db
from route_utils import parse_object_id
from terms_of_use import get_terms

router = APIRouter(tags=["admin"])


# ─── Public ──────────────────────────────────────────────────────────────
@router.get("/terms")
async def public_terms():
    """Public Terms of Use. Returned as markdown for /terms page rendering."""
    return get_terms()


# ─── Guards / helpers ────────────────────────────────────────────────────
def _admin_only(user):
    from auth import require_admin
    require_admin(user)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _audit(db, *, actor_email: str, action: str, target_kind: str,
                 target_id: str, target_label: str = "", reason: str = "",
                 meta: Optional[dict] = None):
    from audit_chain import append_chained
    await append_chained(db, {
        "actor_email": actor_email,
        "action": action,
        "target_kind": target_kind,         # "user" | "affiliate"
        "target_id": target_id,
        "target_label": target_label,
        "reason": (reason or "")[:500],
        "meta": meta or {},
        "at": _now_iso(),
    })


def _serialize_user(u: dict) -> dict:
    return {
        "id": str(u.get("_id") or u.get("id")),
        "email": u.get("email"),
        "name": u.get("name"),
        "role": u.get("role", "user"),
        "status": u.get("status", "active"),
        "created_at": u.get("created_at"),
        "suspension_reason": u.get("suspension_reason"),
        "suspended_at": u.get("suspended_at"),
        "suspended_by": u.get("suspended_by"),
        "terminated_reason": u.get("terminated_reason"),
        "terminated_at": u.get("terminated_at"),
        "terminated_by": u.get("terminated_by"),
        "two_factor_enabled": bool(u.get("two_factor_enabled", False)),
        "referred_by_code": u.get("referred_by_code"),
    }


# ─── Admin · Audit chain integrity + runbooks ────────────────────────────
@router.get("/admin/audit/verify")
async def admin_audit_verify(user=Depends(get_current_user)):
    _admin_only(user)
    from audit_chain import verify_chain
    return await verify_chain(get_db())


@router.get("/admin/execution-brakes")
async def admin_execution_brakes(user=Depends(get_current_user)):
    """R-5 — every account with an active execution brake, for the admin Resume list."""
    _admin_only(user)
    from execution_health import braked_accounts
    return {"accounts": await braked_accounts(get_db())}


@router.get("/admin/runbooks")
async def admin_runbooks(user=Depends(get_current_user)):
    _admin_only(user)
    from runbooks_content import get_runbooks
    return get_runbooks()


# ─── Admin · Platform security settings ─────────────────────────────────
@router.get("/admin/settings/login-otp")
async def admin_get_login_otp(user=Depends(get_current_user)):
    _admin_only(user)
    from login_otp import is_enabled
    db = get_db()
    return {"enabled": await is_enabled(db)}


@router.post("/admin/settings/login-otp")
async def admin_set_login_otp(payload: dict, user=Depends(get_current_user)):
    _admin_only(user)
    from login_otp import set_enabled, is_enabled
    db = get_db()
    enabled = bool(payload.get("enabled"))
    await set_enabled(db, enabled, actor_email=user.get("email", ""))
    await _audit(db, actor_email=user.get("email", ""),
                 action="login_otp_" + ("enabled" if enabled else "disabled"),
                 target_kind="platform", target_id="login_email_otp")
    return {"enabled": await is_enabled(db)}


@router.get("/admin/settings/turnstile")
async def admin_get_turnstile(user=Depends(get_current_user)):
    _admin_only(user)
    from turnstile_gate import is_enabled, secret_key, site_key
    db = get_db()
    return {"enabled": await is_enabled(db),
            "configured": bool(secret_key() and site_key())}


@router.post("/admin/settings/turnstile")
async def admin_set_turnstile(payload: dict, user=Depends(get_current_user)):
    _admin_only(user)
    from turnstile_gate import set_enabled, is_enabled, secret_key, site_key
    db = get_db()
    enabled = bool(payload.get("enabled"))
    if enabled and not (secret_key() and site_key()):
        raise HTTPException(
            status_code=400,
            detail={"code": "turnstile_not_configured",
                    "message": "Set TURNSTILE_SITE_KEY and "
                               "TURNSTILE_SECRET_KEY before enabling."})
    await set_enabled(db, enabled, actor_email=user.get("email", ""))
    await _audit(db, actor_email=user.get("email", ""),
                 action="turnstile_" + ("enabled" if enabled else "disabled"),
                 target_kind="platform", target_id="turnstile")
    return {"enabled": await is_enabled(db),
            "configured": bool(secret_key() and site_key())}


# ─── Admin · Turnstile break-glass (round 9 P1-05) ──────────────────────
@router.get("/admin/settings/turnstile/break-glass")
async def admin_break_glass_status(user=Depends(get_current_user)):
    _admin_only(user)
    from turnstile_break_glass import status
    return await status(get_db())


@router.post("/admin/settings/turnstile/break-glass")
async def admin_break_glass_request(payload: dict, request: Request, user=Depends(get_current_user)):
    """Round 10 P1-06: step-up MFA + records a REQUEST; a second admin must approve."""
    _admin_only(user)
    from step_up import require_step_up
    db = get_db()
    await require_step_up(db, user, request, "authority_relax")
    from turnstile_break_glass import request_activation
    return await request_activation(db, payload or {}, user.get("email", ""))


@router.post("/admin/settings/turnstile/break-glass/approve")
async def admin_break_glass_approve(request: Request, user=Depends(get_current_user)):
    _admin_only(user)
    from step_up import require_step_up
    db = get_db()
    await require_step_up(db, user, request, "authority_relax")
    from turnstile_break_glass import approve_activation
    return await approve_activation(db, user.get("email", ""))


@router.post("/admin/settings/turnstile/break-glass/cancel")
async def admin_break_glass_cancel(request: Request, payload: dict | None = None, user=Depends(get_current_user)):
    _admin_only(user)
    from step_up import require_step_up
    db = get_db()
    await require_step_up(db, user, request, "authority_relax")
    from turnstile_break_glass import cancel_request
    return await cancel_request(db, user.get("email", ""), str((payload or {}).get("note") or ""))


@router.post("/admin/settings/turnstile/break-glass/deactivate")
async def admin_break_glass_deactivate(request: Request, payload: dict | None = None, user=Depends(get_current_user)):
    _admin_only(user)
    from step_up import require_step_up
    db = get_db()
    await require_step_up(db, user, request, "authority_relax")
    from turnstile_break_glass import deactivate
    return await deactivate(db, user.get("email", ""), str((payload or {}).get("note") or ""))


@router.post("/admin/settings/turnstile/break-glass/review")
async def admin_break_glass_review(payload: dict, request: Request, user=Depends(get_current_user)):
    _admin_only(user)
    from step_up import require_step_up
    db = get_db()
    await require_step_up(db, user, request, "authority_relax")
    from turnstile_break_glass import review
    return await review(db, user.get("email", ""), str((payload or {}).get("note") or ""))


# ─── Admin · Users ───────────────────────────────────────────────────────
@router.get("/admin/users")
async def admin_list_users(status: str = "", q: str = "",
                           limit: int = 200, user=Depends(get_current_user)):
    _admin_only(user)
    db = get_db()
    query: dict = {}
    if status:
        query["status"] = status if status != "active" else {"$in": ["active", None]}
        # treat missing status as active
        if status == "active":
            query = {"$or": [{"status": "active"},
                             {"status": {"$exists": False}}]}
    if q:
        # SEC hardening — anchor + escape the user-supplied term so it can't
        # be used as an unbounded regex (ReDoS) even from the admin surface.
        import re
        query["email"] = {"$regex": re.escape(q.strip()), "$options": "i"}
    cursor = db.users.find(query).sort("created_at", -1).limit(min(max(limit, 1), 500))
    docs = await cursor.to_list(length=limit)
    out = []
    for d in docs:
        s = _serialize_user(d)
        # enrich with counts
        s["account_count"] = await db.accounts.count_documents({"user_id": s["id"]})
        s["bot_config_count"] = await db.bot_configs.count_documents({"user_id": s["id"]})
        s["is_affiliate"] = bool(await db.affiliates.find_one({"user_id": s["id"]}))
        out.append(s)
    return {"users": out, "count": len(out)}


@router.get("/admin/users/{user_id}")
async def admin_user_detail(user_id: str, user=Depends(get_current_user)):
    _admin_only(user)
    db = get_db()
    oid = parse_object_id(user_id, "User")
    u = await db.users.find_one({"_id": oid})
    if not u:
        raise HTTPException(status_code=404, detail="user not found")
    base = _serialize_user(u)
    base["accounts"] = await db.accounts.count_documents({"user_id": user_id})
    base["bot_configs"] = await db.bot_configs.count_documents({"user_id": user_id})
    base["affiliate"] = await db.affiliates.find_one({"user_id": user_id}) and True
    audit_cursor = db.admin_audit_log.find(
        {"target_id": user_id, "target_kind": "user"}
    ).sort("at", -1).limit(50)
    audit = await audit_cursor.to_list(length=50)
    for a in audit:
        a["id"] = str(a.pop("_id"))
    base["audit"] = audit
    return base


async def _set_user_status(db, *, user_id: str, new_status: str, reason: str,
                           actor_email: str):
    """Apply suspension or termination side effects atomically-ish."""
    oid = parse_object_id(user_id, "User")
    target = await db.users.find_one({"_id": oid})
    if not target:
        raise HTTPException(status_code=404, detail="user not found")
    if target.get("role") == "admin" and new_status in ("suspended", "terminated"):
        raise HTTPException(status_code=400,
                            detail="cannot suspend or terminate an admin")

    now = _now_iso()
    set_doc: dict = {"status": new_status}
    if new_status == "suspended":
        set_doc.update({
            "suspension_reason": reason,
            "suspended_at": now,
            "suspended_by": actor_email,
        })
    elif new_status == "terminated":
        set_doc.update({
            "terminated_reason": reason,
            "terminated_at": now,
            "terminated_by": actor_email,
        })
    elif new_status == "active":
        # restoration — clear suspension fields but keep terminated_* for history
        set_doc.update({
            "suspension_reason": None,
            "suspended_at": None,
            "suspended_by": None,
        })
    await db.users.update_one({"_id": oid}, {"$set": set_doc})

    # Side effects: stop the bot for this user
    if new_status in ("suspended", "terminated"):
        await db.bot_configs.update_many(
            {"user_id": user_id, "active": True},
            {"$set": {
                "active": False,
                "deactivated_reason": f"user_{new_status}",
                "deactivated_at": now,
            }},
        )
        # If user is an affiliate, also deactivate the affiliate row
        await db.affiliates.update_many(
            {"user_id": user_id},
            {"$set": {
                "active": False,
                "suspended_reason": reason,
                "suspended_at": now,
                "suspended_by": actor_email,
                **({"terminated": True, "terminated_at": now,
                    "terminated_by": actor_email}
                   if new_status == "terminated" else {}),
            }},
        )

    return _serialize_user({**target, **set_doc, "_id": oid})


@router.post("/admin/users/{user_id}/suspend")
async def admin_suspend_user(user_id: str, payload: dict,
                             user=Depends(get_current_user)):
    _admin_only(user)
    reason = (payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason required")
    db = get_db()
    from security import revoke_user_api_keys
    await revoke_user_api_keys(db, user_id, "account_suspended")          # fix plan S10
    result = await _set_user_status(
        db, user_id=user_id, new_status="suspended",
        reason=reason, actor_email=user.get("email") or "",
    )
    await _audit(db, actor_email=user.get("email") or "",
                 action="suspend", target_kind="user",
                 target_id=user_id, target_label=result.get("email") or "",
                 reason=reason)
    return {"ok": True, "user": result}


@router.post("/admin/users/{user_id}/unsuspend")
async def admin_unsuspend_user(user_id: str, user=Depends(get_current_user)):
    _admin_only(user)
    db = get_db()
    oid = parse_object_id(user_id, "User")
    target = await db.users.find_one({"_id": oid})
    if not target:
        raise HTTPException(status_code=404, detail="user not found")
    if (target.get("status") or "active") != "suspended":
        raise HTTPException(status_code=400, detail="user is not suspended")
    result = await _set_user_status(
        db, user_id=user_id, new_status="active",
        reason="", actor_email=user.get("email") or "",
    )
    await _audit(db, actor_email=user.get("email") or "",
                 action="unsuspend", target_kind="user",
                 target_id=user_id, target_label=result.get("email") or "")
    return {"ok": True, "user": result}


@router.post("/admin/users/{user_id}/terminate")
async def admin_terminate_user(user_id: str, payload: dict,
                                user=Depends(get_current_user)):
    _admin_only(user)
    reason = (payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason required")
    db = get_db()
    result = await _set_user_status(
        db, user_id=user_id, new_status="terminated",
        reason=reason, actor_email=user.get("email") or "",
    )
    await _audit(db, actor_email=user.get("email") or "",
                 action="terminate", target_kind="user",
                 target_id=user_id, target_label=result.get("email") or "",
                 reason=reason)
    return {"ok": True, "user": result}


@router.post("/admin/users/{user_id}/restore")
async def admin_restore_user(user_id: str, user=Depends(get_current_user)):
    _admin_only(user)
    db = get_db()
    oid = parse_object_id(user_id, "User")
    target = await db.users.find_one({"_id": oid})
    if not target:
        raise HTTPException(status_code=404, detail="user not found")
    if (target.get("status") or "active") != "terminated":
        raise HTTPException(status_code=400, detail="user is not terminated")
    result = await _set_user_status(
        db, user_id=user_id, new_status="active",
        reason="", actor_email=user.get("email") or "",
    )
    await _audit(db, actor_email=user.get("email") or "",
                 action="restore", target_kind="user",
                 target_id=user_id, target_label=result.get("email") or "")
    return {"ok": True, "user": result}


# ─── Admin · Affiliates ──────────────────────────────────────────────────
def _serialize_affiliate(a: dict) -> dict:
    return {
        "id": str(a.get("_id") or a.get("id")),
        "user_id": a.get("user_id"),
        "user_email": a.get("user_email"),
        "full_name": a.get("full_name"),
        "code": a.get("code"),
        "active": bool(a.get("active", True)),
        "terminated": bool(a.get("terminated", False)),
        "suspended_reason": a.get("suspended_reason"),
        "suspended_at": a.get("suspended_at"),
        "suspended_by": a.get("suspended_by"),
        "terminated_at": a.get("terminated_at"),
        "terminated_by": a.get("terminated_by"),
        "terminated_reason": a.get("terminated_reason"),
        "forfeited_balance_usd": a.get("forfeited_balance_usd"),
        "created_at": a.get("created_at"),
        "lifetime_clicks": a.get("lifetime_clicks", 0),
        "lifetime_conversions": a.get("lifetime_conversions", 0),
        "lifetime_earnings_usd": a.get("lifetime_earnings_usd", 0.0),
        "unpaid_balance_usd": a.get("unpaid_balance_usd", 0.0),
        "payment_method": a.get("payment_method"),
    }


@router.get("/admin/affiliates")
async def admin_list_affiliates(status: str = "", user=Depends(get_current_user)):
    """status filter: 'active' | 'suspended' | 'terminated' | '' (all)."""
    _admin_only(user)
    db = get_db()
    query: dict = {}
    if status == "active":
        query = {"active": True, "terminated": {"$ne": True}}
    elif status == "suspended":
        query = {"active": False, "terminated": {"$ne": True}}
    elif status == "terminated":
        query = {"terminated": True}
    cursor = db.affiliates.find(query).sort("created_at", -1).limit(500)
    docs = await cursor.to_list(length=500)
    return {"affiliates": [_serialize_affiliate(d) for d in docs]}


async def _set_affiliate_state(db, *, aff_id: str, new_state: str, reason: str,
                                actor_email: str):
    oid = parse_object_id(aff_id, "Affiliate")
    target = await db.affiliates.find_one({"_id": oid})
    if not target:
        raise HTTPException(status_code=404, detail="affiliate not found")

    now = _now_iso()
    if new_state == "suspended":
        set_doc = {
            "active": False,
            "suspended_reason": reason,
            "suspended_at": now,
            "suspended_by": actor_email,
        }
    elif new_state == "terminated":
        # Terminate: deactivate AND forfeit unpaid balance per Terms §7.
        forfeit = float(target.get("unpaid_balance_usd") or 0.0)
        set_doc = {
            "active": False,
            "terminated": True,
            "terminated_reason": reason,
            "terminated_at": now,
            "terminated_by": actor_email,
            "unpaid_balance_usd": 0.0,
            "unpaid_balance_cents": 0,
            "forfeited_balance_usd": forfeit,
        }
        # Cancel any pending payout requests for this affiliate.
        await db.affiliate_payout_requests.update_many(
            {"affiliate_id": str(oid), "status": "pending"},
            {"$set": {"status": "cancelled",
                      "cancelled_reason": "affiliate_terminated",
                      "cancelled_at": now}},
        )
    elif new_state == "active":
        set_doc = {
            "active": True,
            "suspended_reason": None,
            "suspended_at": None,
            "suspended_by": None,
        }
    else:
        raise HTTPException(status_code=400, detail="invalid state")

    await db.affiliates.update_one({"_id": oid}, {"$set": set_doc})
    fresh = await db.affiliates.find_one({"_id": oid})
    return _serialize_affiliate(fresh)


@router.post("/admin/affiliates/{aff_id}/suspend")
async def admin_suspend_affiliate(aff_id: str, payload: dict,
                                   user=Depends(get_current_user)):
    _admin_only(user)
    reason = (payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason required")
    db = get_db()
    out = await _set_affiliate_state(
        db, aff_id=aff_id, new_state="suspended",
        reason=reason, actor_email=user.get("email") or "",
    )
    await _audit(db, actor_email=user.get("email") or "",
                 action="suspend", target_kind="affiliate",
                 target_id=aff_id, target_label=out.get("user_email") or out.get("code") or "",
                 reason=reason)
    return {"ok": True, "affiliate": out}


@router.post("/admin/affiliates/{aff_id}/unsuspend")
async def admin_unsuspend_affiliate(aff_id: str, user=Depends(get_current_user)):
    _admin_only(user)
    db = get_db()
    oid = parse_object_id(aff_id, "Affiliate")
    target = await db.affiliates.find_one({"_id": oid})
    if not target:
        raise HTTPException(status_code=404, detail="affiliate not found")
    if target.get("terminated"):
        raise HTTPException(status_code=400,
                            detail="affiliate is terminated, cannot unsuspend")
    if target.get("active"):
        raise HTTPException(status_code=400, detail="affiliate is already active")
    out = await _set_affiliate_state(
        db, aff_id=aff_id, new_state="active",
        reason="", actor_email=user.get("email") or "",
    )
    await _audit(db, actor_email=user.get("email") or "",
                 action="unsuspend", target_kind="affiliate",
                 target_id=aff_id, target_label=out.get("user_email") or "")
    return {"ok": True, "affiliate": out}


@router.post("/admin/affiliates/{aff_id}/terminate")
async def admin_terminate_affiliate(aff_id: str, payload: dict,
                                     user=Depends(get_current_user)):
    _admin_only(user)
    reason = (payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason required")
    db = get_db()
    out = await _set_affiliate_state(
        db, aff_id=aff_id, new_state="terminated",
        reason=reason, actor_email=user.get("email") or "",
    )
    await _audit(db, actor_email=user.get("email") or "",
                 action="terminate", target_kind="affiliate",
                 target_id=aff_id, target_label=out.get("user_email") or "",
                 reason=reason,
                 meta={"forfeited_usd": out.get("forfeited_balance_usd", 0.0)})
    return {"ok": True, "affiliate": out}


# ─── Admin · Audit ───────────────────────────────────────────────────────
@router.get("/admin/audit-log")
async def admin_audit_log(limit: int = 200, kind: str = "",
                          user=Depends(get_current_user)):
    _admin_only(user)
    db = get_db()
    q = {"target_kind": kind} if kind else {}
    cursor = db.admin_audit_log.find(q).sort("at", -1).limit(min(max(limit, 1), 500))
    docs = await cursor.to_list(length=limit)
    for d in docs:
        d["id"] = str(d.pop("_id"))
    return {"audit": docs}


# ── Admin → Integrations (status · live tests · sealed secret updates with re-auth) ──
@router.get("/admin/integrations")
async def admin_integrations_status(user=Depends(get_current_user)):
    from auth import require_admin
    require_admin(user)
    import integrations_settings as integ
    return await integ.status(get_db())


@router.post("/admin/integrations/test/{provider}")
async def admin_integrations_test(provider: str, user=Depends(get_current_user)):
    from auth import require_admin
    require_admin(user)
    import integrations_settings as integ
    if provider not in integ.PROVIDERS:
        raise HTTPException(status_code=404, detail="unknown provider")
    return await integ.test_provider(get_db(), provider, user)


async def _reauth(db, user: dict, password: str, otp: str | None) -> None:
    """Secret changes need fresh proof of the admin's password (+ TOTP when enrolled)."""
    from auth import verify_password
    from security import rate_limit
    await rate_limit(db, "admin_reauth", user["id"], 5, 300, "Too many re-authentication attempts")
    doc = await db.users.find_one({"_id": ObjectId(user["id"])})
    if not doc or not password or not verify_password(password, doc.get("password_hash") or ""):
        raise HTTPException(status_code=401, detail={"code": "reauth_failed", "message": "Password incorrect"})
    if doc.get("two_factor_enabled"):
        from totp import verify_code_once
        if not otp or not await verify_code_once(db, user["id"], doc.get("totp_secret") or "", str(otp)):
            raise HTTPException(status_code=401, detail={"code": "reauth_failed", "message": "Authenticator code incorrect"})


@router.post("/admin/integrations/secret")
async def admin_integrations_secret(payload: dict, user=Depends(get_current_user)):
    from auth import require_admin
    require_admin(user)
    import integrations_settings as integ
    db = get_db()
    key = str(payload.get("key") or "")
    if key not in integ.REGISTRY:
        raise HTTPException(status_code=404, detail="unknown key")
    await _reauth(db, user, str(payload.get("password") or ""), payload.get("otp"))
    try:
        return await integ.update_secret(db, key, str(payload.get("value") or ""), user)
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"code": "invalid_value", "message": str(e)})


@router.post("/admin/integrations/rewrap")
async def admin_vault_rewrap(payload: dict, user=Depends(get_current_user)):
    """audit r29 P2-04 — re-seal every vault secret under the current master key (re-auth)."""
    from auth import require_admin
    from audit_chain import append_chained
    import integrations_settings as integ
    require_admin(user)
    db = get_db()
    await _reauth(db, user, str(payload.get("password") or ""), payload.get("otp"))
    out = await integ.rewrap_all(db, user)
    await append_chained(db, {"actor_email": user.get("email"), "action": "vault_rewrap", "target_kind": "vault",
                              "target_id": out["manifest_id"], "reason": payload.get("reason") or "key rotation",
                              "meta": {"status": out["status"], "total": out["total"], "failed": out["failed"],
                                       "to_key_version": out["to_key_version"], "reauth": True}, "at": _now_iso()})
    return out


# ── Admin → Broker Registry → Account Environments (server-authoritative DEMO attestation) ──
def _env_row(a: dict) -> dict:
    from broker_env import attestation_state, attested_environment, broker_environment, demo_proof
    att = a.get("environment_attestation") or {}
    proof = demo_proof(a)
    return {"account_id": str(a["_id"]), "user_id": a.get("user_id"), "label": a.get("label"),
            "broker": a.get("broker"), "server": a.get("broker_server") or a.get("server"),
            "account_number": a.get("account_number"), "account_type": a.get("account_type"),
            "declared": broker_environment(a), "effective": attested_environment(a),
            "attestation_state": attestation_state(a),
            "attested_by": att.get("approved_by"), "attested_at": att.get("at"), "reason": att.get("reason"),
            "identity_hash": att.get("identity_hash"), "verifier": (att.get("proof") or {}).get("verifier"),
            "proof": proof}


_ENV_PROJECTION = {"user_id": 1, "label": 1, "broker": 1, "broker_server": 1, "server": 1,
                   "account_number": 1, "account_type": 1, "broker_environment": 1, "mode": 1,
                   "environment_attestation": 1, "ea_identity": 1, "broker_account_id_reported": 1,
                   "creds_version": 1, "last_heartbeat": 1, "broker_account_mismatch": 1}


@router.get("/admin/account-environments")
async def admin_account_environments(user=Depends(get_current_user)):
    from auth import require_admin
    require_admin(user)
    db = get_db()
    accs = await db.accounts.find({"mode": {"$ne": "paper"}}, _ENV_PROJECTION).sort("_id", -1).to_list(500)
    rows = [_env_row(a) for a in accs]
    return {"accounts": [r for r in rows if r["declared"] == "DEMO" or r["attested_by"]]}


@router.post("/admin/account-environments/{account_id}")
async def admin_attest_account_environment(account_id: str, payload: dict, user=Depends(get_current_user)):
    """Attest (or revoke) DEMO for an account. Re-auth required; the attestation is bound to the
    account's identity digest (broker_env.attestation_identity) and only takes effect while the
    declared classification is DEMO and the digest is unchanged."""
    from auth import require_admin
    from broker_env import attestation_identity, broker_environment, demo_proof
    from audit_chain import append_chained
    require_admin(user)
    db = get_db()
    acc = await db.accounts.find_one({"_id": parse_object_id(account_id, "Account")})
    if not acc:
        raise HTTPException(status_code=404, detail="account not found")
    env = str(payload.get("environment") or "").upper()
    if env not in ("DEMO", "LIVE"):
        raise HTTPException(status_code=422, detail={"code": "invalid_value", "message": "environment must be DEMO or LIVE"})
    reason = str(payload.get("reason") or "").strip()[:300]
    override = bool(payload.get("override"))
    await _reauth(db, user, str(payload.get("password") or ""), payload.get("otp"))
    proof = demo_proof(acc)
    if env == "DEMO" and broker_environment(acc) != "DEMO":
        raise HTTPException(status_code=409, detail={
            "code": "declared_not_demo",
            "message": "the account's declared classification is not DEMO — attestation cannot downgrade it"})
    if env == "DEMO" and not proof["mandatory_ok"]:
        failed = [k for k, v in proof["checks"].items() if not v and k != "server_demo_named"]
        raise HTTPException(status_code=409, detail={
            "code": "demo_proof_missing", "failed_checks": failed, "proof": proof,
            "message": "DEMO cannot be attested without fresh authoritative terminal evidence: " + ", ".join(failed)})
    if env == "DEMO" and not proof["ok"]:
        if not override:
            raise HTTPException(status_code=409, detail={
                "code": "server_not_demo_named", "proof": proof,
                "message": f"EA reports server '{proof['reported_server']}' which is not demo-named — "
                           "an explicit admin override (audited, shown in red) is required"})
        if len(reason) < 10:
            raise HTTPException(status_code=422, detail={"code": "override_reason_required",
                                                         "message": "an override needs a reason (≥10 chars)"})
    verifier = "ea_heartbeat" if proof["ok"] else "admin_override"
    now = _now_iso()
    if env == "DEMO":
        att = {"environment": "DEMO", "approved_by": user.get("email"), "at": now, "reason": reason,
               "identity_hash": attestation_identity(acc),
               "proof": {"proof_id": proof["proof_id"], "verifier": verifier, "checks": proof["checks"],
                         "heartbeat_age_s": proof["heartbeat_age_s"], "reported_server": proof["reported_server"],
                         "at": now}}
        await db.accounts.update_one({"_id": acc["_id"]}, {"$set": {"environment_attestation": att}})
    else:
        await db.accounts.update_one({"_id": acc["_id"]}, {"$unset": {"environment_attestation": ""}})
    await append_chained(db, {"actor_email": user.get("email"), "action": "account_environment_attest",
                              "target_kind": "account", "target_id": str(acc["_id"]),
                              "target_label": acc.get("label"), "reason": reason or env,
                              "meta": {"environment": env, "declared": broker_environment(acc), "reauth": True,
                                       "identity_hash": attestation_identity(acc), "proof_id": proof["proof_id"],
                                       "verifier": verifier if env == "DEMO" else None,
                                       "override": override and env == "DEMO", "checks": proof["checks"]},
                              "at": now})
    acc = await db.accounts.find_one({"_id": acc["_id"]})
    return _env_row(acc)


# ── A6/H1 — position mode (netting vs hedging) ───────────────────────────────
_PM_PROJECTION = {"user_id": 1, "label": 1, "broker": 1, "broker_server": 1, "server": 1,
                  "account_number": 1, "account_type": 1, "mode": 1, "position_mode_override": 1,
                  "margin_mode": 1, "ea_identity.margin_mode": 1}


async def _position_mode_row(db, a: dict) -> dict:
    from routes.bridge_routes import position_mode_resolution
    res = await position_mode_resolution(db, a)
    return {"account_id": str(a["_id"]), "user_id": a.get("user_id"), "label": a.get("label"),
            "broker": a.get("broker"), "server": a.get("broker_server") or a.get("server"),
            "account_number": a.get("account_number"), "account_type": a.get("account_type"),
            "mode": res["mode"], "source": res["source"], "resolution": res,
            "override": a.get("position_mode_override")}


@router.get("/admin/account-position-modes")
async def admin_account_position_modes(user=Depends(get_current_user)):
    """Netting/hedging verdict per broker account with its source (admin | ea | registry | default).
    The EA (≤ v1.57) never reports ACCOUNT_MARGIN_MODE — a netting broker missing from the
    registry is treated as hedging unless an admin sets the mode here."""
    from auth import require_admin
    require_admin(user)
    db = get_db()
    accs = await db.accounts.find({"mode": {"$ne": "paper"}}, _PM_PROJECTION).sort("_id", -1).to_list(500)
    return {"accounts": [await _position_mode_row(db, a) for a in accs]}


@router.post("/admin/account-position-modes/{account_id}")
async def admin_set_account_position_mode(account_id: str, payload: dict, user=Depends(get_current_user)):
    """Set (netting | hedging) or clear (auto → registry/default) the account's position mode.
    Re-auth required; recorded in the admin audit chain."""
    from auth import require_admin
    from audit_chain import append_chained
    require_admin(user)
    db = get_db()
    acc = await db.accounts.find_one({"_id": parse_object_id(account_id, "Account")})
    if not acc:
        raise HTTPException(status_code=404, detail="account not found")
    mode = str(payload.get("mode") or "").lower()
    if mode not in ("netting", "hedging", "auto"):
        raise HTTPException(status_code=422, detail={"code": "invalid_value",
                                                     "message": "mode must be netting, hedging or auto"})
    reason = str(payload.get("reason") or "").strip()[:300]
    await _reauth(db, user, str(payload.get("password") or ""), payload.get("otp"))
    now = _now_iso()
    before = (await _position_mode_row(db, acc))["resolution"]
    # N13 — the audit entry is written BEFORE the change (intent), so a failed
    # or interrupted write can never leave an unaudited position-mode change.
    await append_chained(db, {"actor_email": user.get("email"), "action": "account_position_mode_set",
                              "target_kind": "account", "target_id": str(acc["_id"]),
                              "target_label": acc.get("label"), "reason": reason or mode,
                              "meta": {"mode": mode, "before": before, "reauth": True, "phase": "intent"},
                              "at": now})
    try:
        if mode == "auto":
            await db.accounts.update_one({"_id": acc["_id"]}, {"$unset": {"position_mode_override": ""}})
        else:
            await db.accounts.update_one({"_id": acc["_id"]}, {"$set": {"position_mode_override": {
                "mode": mode, "by": user.get("email"), "at": now, "reason": reason}}})
    except Exception as e:  # noqa: BLE001
        await append_chained(db, {"actor_email": user.get("email"), "action": "account_position_mode_set_failed",
                                  "target_kind": "account", "target_id": str(acc["_id"]),
                                  "reason": type(e).__name__, "meta": {"mode": mode, "phase": "failed"},
                                  "at": _now_iso()})
        raise
    acc = await db.accounts.find_one({"_id": acc["_id"]})
    row = await _position_mode_row(db, acc)
    await append_chained(db, {"actor_email": user.get("email"), "action": "account_position_mode_set",
                              "target_kind": "account", "target_id": str(acc["_id"]),
                              "target_label": acc.get("label"), "reason": reason or mode,
                              "meta": {"mode": mode, "before": before, "after": row["resolution"],
                                       "reauth": True, "phase": "applied"},
                              "at": _now_iso()})
    return row
@router.get("/admin/integrations/plans")
async def admin_plans_get(user=Depends(get_current_user)):
    from auth import require_admin
    require_admin(user)
    import plan_settings
    return plan_settings.current()


@router.post("/admin/integrations/plans")
async def admin_plans_update(payload: dict, user=Depends(get_current_user)):
    from auth import require_admin
    require_admin(user)
    import plan_settings
    db = get_db()
    await _reauth(db, user, str(payload.get("password") or ""), payload.get("otp"))
    try:
        return await plan_settings.update(db, payload, user)
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"code": "invalid_value", "message": str(e)})


# ── Admin → Integrations → transactional e-mail templates (preview · test send) ──
@router.get("/admin/integrations/email-templates")
async def admin_email_templates(user=Depends(get_current_user)):
    from auth import require_admin
    require_admin(user)
    import email_templates
    from email_sender import is_configured, _sender
    return {"templates": email_templates.catalog(), "configured": is_configured(), "sender": _sender()}


@router.get("/admin/integrations/email-templates/{template_id}")
async def admin_email_template_preview(template_id: str, user=Depends(get_current_user)):
    from auth import require_admin
    require_admin(user)
    import email_templates
    try:
        return email_templates.render(template_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown template")


@router.post("/admin/integrations/email-templates/{template_id}/send")
async def admin_email_template_send(template_id: str, payload: dict, user=Depends(get_current_user)):
    from auth import require_admin
    require_admin(user)
    import email_templates
    from security import rate_limit
    if template_id not in email_templates.REGISTRY:
        raise HTTPException(status_code=404, detail="unknown template")
    recipient = str(payload.get("recipient") or user.get("email") or "").strip().lower()
    if "@" not in recipient or len(recipient) > 254:
        raise HTTPException(status_code=422, detail={"code": "invalid_value", "message": "invalid recipient"})
    db = get_db()
    await rate_limit(db, "admin_template_send", user["id"], 10, 600, "Too many test e-mails — wait a few minutes")
    res = await email_templates.send_test(template_id, recipient)
    await _audit(db, actor_email=user.get("email", ""), action="email_template_test_send", target_kind="email_template",
                 target_id=template_id, reason=f"to {recipient} · {'sent' if res.get('ok') else 'failed'}")
    if not res.get("ok"):
        raise HTTPException(status_code=503, detail={"code": "email_send_failed", "message": str(res.get("error"))})
    return res
