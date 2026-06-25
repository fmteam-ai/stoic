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
from route_utils import parse_object_id
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


# --- Self-service payout requests -----------------------------------------
@router.post("/affiliate/request-payout")
async def request_payout(user=Depends(get_current_user)):
    """Affiliate self-requests a payout. Requires unpaid balance ≥ $50.

    Creates a row in `affiliate_payout_requests`. Admin processes it via
    POST /admin/affiliate/payout-requests/{id}/process which marks the
    underlying commissions paid and zeros the affiliate's unpaid_balance.
    """
    db = get_db()
    aff = await db.affiliates.find_one({"user_id": user["id"], "active": True})
    if not aff:
        raise HTTPException(status_code=404, detail="No active affiliate profile")
    balance = float(aff.get("unpaid_balance_usd") or 0.0)
    MIN_PAYOUT_USD = 50.0
    if balance < MIN_PAYOUT_USD:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum payout is ${MIN_PAYOUT_USD:.0f} — current balance ${balance:.2f}",
        )
    # Block multiple open requests
    pending = await db.affiliate_payout_requests.find_one(
        {"affiliate_id": str(aff["_id"]), "status": "pending"}
    )
    if pending:
        raise HTTPException(
            status_code=400,
            detail="A payout request is already pending — admin will process it shortly.",
        )
    doc = {
        "affiliate_id": str(aff["_id"]),
        "affiliate_code": aff.get("code"),
        "user_id": user["id"],
        "user_email": user.get("email"),
        "amount_usd": round(balance, 2),
        "payment_method": aff.get("payment_method"),
        "payment_details": aff.get("payment_details"),
        "status": "pending",
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    r = await db.affiliate_payout_requests.insert_one(doc)
    return {"ok": True, "id": str(r.inserted_id), "amount_usd": doc["amount_usd"]}


@router.get("/affiliate/payout-requests")
async def list_my_payout_requests(user=Depends(get_current_user)):
    """An affiliate's own payout-request history."""
    db = get_db()
    aff = await db.affiliates.find_one({"user_id": user["id"]})
    if not aff:
        return {"requests": []}
    cursor = db.affiliate_payout_requests.find(
        {"affiliate_id": str(aff["_id"])}
    ).sort("requested_at", -1).limit(20)
    docs = await cursor.to_list(length=20)
    for d in docs:
        d["id"] = str(d.pop("_id"))
    return {"requests": docs}


@router.get("/admin/affiliate/payout-requests")
async def admin_list_payout_requests(status: str = "pending",
                                      user=Depends(get_current_user)):
    _admin_only(user)
    db = get_db()
    q = {"status": status} if status else {}
    cursor = db.affiliate_payout_requests.find(q).sort("requested_at", -1).limit(200)
    docs = await cursor.to_list(length=200)
    for d in docs:
        d["id"] = str(d.pop("_id"))
    return {"requests": docs}


@router.post("/admin/affiliate/payout-requests/{rid}/process")
async def admin_process_payout(rid: str, user=Depends(get_current_user)):
    """Admin marks a payout as paid: flips all pending commissions to 'paid'
    and zeros out the affiliate's unpaid_balance.
    """
    _admin_only(user)
    db = get_db()
    oid = parse_object_id(rid, "Payout request")
    req = await db.affiliate_payout_requests.find_one_and_update(
        {"_id": oid, "status": "pending"},
        {"$set": {"status": "paid",
                  "processed_at": datetime.now(timezone.utc).isoformat(),
                  "processed_by": user.get("email")}},
        return_document=ReturnDocument.AFTER,
    )
    if not req:
        raise HTTPException(status_code=404, detail="not found or already processed")
    # Flip all pending commissions for this affiliate to paid
    await db.affiliate_commissions.update_many(
        {"affiliate_id": req["affiliate_id"], "status": "pending"},
        {"$set": {"status": "paid",
                  "paid_at": datetime.now(timezone.utc).isoformat(),
                  "paid_by": user.get("email"),
                  "payout_request_id": rid}},
    )
    # Zero out the running unpaid balance
    try:
        aff_oid = ObjectId(req["affiliate_id"])
    except Exception:
        logger.error("Corrupt affiliate_id in payout request %s: %r", rid, req.get("affiliate_id"))
        return {"ok": True, "id": rid, "warning": "affiliate balance not zeroed (id corrupt)"}
    await db.affiliates.update_one(
        {"_id": aff_oid},
        {"$set": {"unpaid_balance_usd": 0.0}},
    )
    return {"ok": True, "id": rid}


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
    oid = parse_object_id(cid, "Commission")
    doc = await db.affiliate_commissions.find_one_and_update(
        {"_id": oid, "status": "pending"},
        {"$set": {"status": "paid", "paid_at": datetime.now(timezone.utc).isoformat(),
                  "paid_by": user.get("email")}},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise HTTPException(status_code=404, detail="not found or already paid")
    try:
        aff_oid = ObjectId(doc["affiliate_id"])
    except Exception:
        logger.error("Corrupt affiliate_id in commission %s: %r", cid, doc.get("affiliate_id"))
        return {"ok": True, "warning": "affiliate balance not updated (id corrupt)"}
    await db.affiliates.update_one(
        {"_id": aff_oid},
        {"$inc": {"unpaid_balance_usd": -float(doc["commission_usd"])}},
    )
    return {"ok": True}
