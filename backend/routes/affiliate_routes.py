"""Affiliate program routes — apply, attribution, dashboard, admin moderation."""
import logging
from pymongo import ReturnDocument
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from auth import get_current_user
from database import get_db
from bson import ObjectId
from datetime import datetime, timezone
from subscription_service import is_active as subscription_active
from affiliate_service import (
    submit_application, get_application, get_affiliate, stats_for,
    record_click, approve_application, reject_application, list_applications,
)

logger = logging.getLogger("affiliate")
router = APIRouter(tags=["affiliate"])


async def _require_active_subscription(user):
    """Affiliate program is gated behind an active paid subscription.

    Admins are grandfathered. Users in 30-day legacy grace can still apply
    (so we don't strand pre-rollout accounts). Everyone else must hold a
    valid paid plan — we return HTTP 402 Payment Required so the frontend
    can route them to the Subscription page.
    """
    if user.get("role") == "admin":
        return
    sub = await subscription_active(user["id"])
    if not sub.get("active"):
        raise HTTPException(
            status_code=402,
            detail={
                "code": "subscription_required",
                "message": "Affiliate program requires an active paid subscription.",
                "valid_until": sub.get("valid_until"),
            },
        )


# --- Public (logged-in user) endpoints ------------------------------------
@router.post("/affiliate/apply")
async def affiliate_apply(payload: dict, user=Depends(get_current_user)):
    await _require_active_subscription(user)
    if not payload.get("terms_agreed"):
        raise HTTPException(status_code=400, detail="Terms must be accepted to apply")
    required = ("full_name", "audience_url", "promotion_strategy", "payment_method")
    for k in required:
        if not (payload.get(k) or "").strip():
            raise HTTPException(status_code=400, detail=f"{k} required")
    body = {
        "user_email": user.get("email"),
        "full_name": payload["full_name"].strip(),
        "audience_url": payload["audience_url"].strip(),
        "audience_size": (payload.get("audience_size") or "").strip(),
        "promotion_strategy": payload["promotion_strategy"].strip()[:2000],
        "payment_method": payload["payment_method"].strip(),
        "payment_details": (payload.get("payment_details") or "").strip()[:500],
        "terms_version": payload.get("terms_version", "2026-06-22"),
    }
    res = await submit_application(user_id=user["id"], payload=body)
    return res


@router.get("/affiliate/status")
async def affiliate_status(user=Depends(get_current_user)):
    # Surface subscription gating to the frontend without blocking — existing
    # affiliates (whose sub later lapsed) still need to see their balance.
    sub = await subscription_active(user["id"])
    sub_required = (user.get("role") != "admin") and (not sub.get("active"))
    affiliate = await get_affiliate(user["id"])
    if affiliate:
        return {"state": "approved", "affiliate": affiliate,
                "subscription_required": sub_required,
                "subscription": sub}
    app_doc = await get_application(user["id"])
    if app_doc:
        return {"state": app_doc["status"], "application": app_doc,
                "subscription_required": sub_required,
                "subscription": sub}
    if sub_required:
        return {"state": "subscription_required", "subscription": sub,
                "subscription_required": True}
    return {"state": "none", "subscription_required": False,
            "subscription": sub}


@router.get("/affiliate/stats")
async def affiliate_stats(user=Depends(get_current_user)):
    return await stats_for(user["id"])


# --- Public attribution redirect ------------------------------------------
@router.get("/r/{code}")
async def referral_redirect(code: str, request: Request):
    """Public unauthenticated redirect that records the click + sets the
    attribution cookie. Lands the visitor on the marketing/register page.
    """
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "")
    if ip and "," in ip:
        ip = ip.split(",")[0].strip()
    user_agent = request.headers.get("user-agent", "")
    referrer = request.headers.get("referer", "")
    affiliate = await record_click(code=code, ip=ip, user_agent=user_agent, referrer=referrer)
    target = "/register" if affiliate else "/"
    response = RedirectResponse(url=target)
    if affiliate:
        # 60-day attribution cookie. Front-end / register endpoint will pick
        # this up and stamp `referred_by_code` + `referred_at` onto the user.
        response.set_cookie(
            key="stoic_ref",
            value=code.upper(),
            max_age=60 * 24 * 3600,
            httponly=False,  # JS readable so frontend can echo into register
            samesite="lax",
            secure=True,
        )
        response.set_cookie(
            key="stoic_ref_at",
            value=datetime.now(timezone.utc).isoformat(),
            max_age=60 * 24 * 3600,
            httponly=False,
            samesite="lax",
            secure=True,
        )
    return response


# --- Admin endpoints ------------------------------------------------------
def _admin_only(user):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin only")


@router.get("/admin/affiliate/applications")
async def admin_list(status: str = "", user=Depends(get_current_user)):
    _admin_only(user)
    return await list_applications(status=status or None)


@router.post("/admin/affiliate/applications/{app_id}/approve")
async def admin_approve(app_id: str, user=Depends(get_current_user)):
    _admin_only(user)
    res = await approve_application(app_id, user.get("email"))
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "approve failed"))
    return res


@router.post("/admin/affiliate/applications/{app_id}/reject")
async def admin_reject(app_id: str, payload: dict, user=Depends(get_current_user)):
    _admin_only(user)
    res = await reject_application(app_id, user.get("email"),
                                   reason=payload.get("reason", "")[:500])
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail="reject failed")
    return res


@router.get("/admin/affiliate/commissions")
async def admin_commissions(status: str = "", user=Depends(get_current_user)):
    _admin_only(user)
    db = get_db()
    q = {"status": status} if status else {}
    cursor = db.affiliate_commissions.find(q).sort("created_at", -1).limit(500)
    docs = await cursor.to_list(length=500)
    for d in docs:
        d["id"] = str(d.pop("_id"))
    return docs


@router.post("/admin/affiliate/commissions/{cid}/mark-paid")
async def admin_mark_paid(cid: str, user=Depends(get_current_user)):
    _admin_only(user)
    db = get_db()
    try:
        oid = ObjectId(cid)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid id")
    doc = await db.affiliate_commissions.find_one_and_update(
        {"_id": oid, "status": "pending"},
        {"$set": {"status": "paid", "paid_at": datetime.now(timezone.utc).isoformat(),
                  "paid_by": user.get("email")}},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise HTTPException(status_code=404, detail="not found or already paid")
    await db.affiliates.update_one(
        {"_id": ObjectId(doc["affiliate_id"])},
        {"$inc": {"unpaid_balance_usd": -float(doc["commission_usd"])}},
    )
    return {"ok": True}
