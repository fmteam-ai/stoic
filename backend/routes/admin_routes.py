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
