"""Subscription + Stripe Checkout routes.

POST /api/subscription/checkout    — create Stripe Checkout session for a plan
GET  /api/subscription/status      — current entitlement
GET  /api/subscription/plans       — public catalog
GET  /api/subscription/poll/{sid}  — poll Stripe status, sync our DB
POST /api/webhook/stripe           — Stripe webhook receiver
"""
import os
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from emergentintegrations.payments.stripe.checkout import (
    StripeCheckout, CheckoutSessionRequest,
)

from auth import get_current_user
from subscription_plans import get_plan, all_plans_public
from subscription_service import (
    get_subscription, is_active, record_transaction, apply_successful_payment,
)
from database import get_db

logger = logging.getLogger("subscription")
router = APIRouter(tags=["subscription"])

# Subscription-level router (auth required)
sub_router = APIRouter(prefix="/subscription")
# Webhook router (no auth — Stripe-Signature verified inside)
hook_router = APIRouter(prefix="/webhook")


def _stripe_client(host_url: str) -> StripeCheckout:
    key = os.environ["STRIPE_API_KEY"]
    webhook_url = f"{host_url.rstrip('/')}/api/webhook/stripe"
    return StripeCheckout(api_key=key, webhook_url=webhook_url)


@sub_router.get("/plans")
async def list_plans():
    return all_plans_public()


@sub_router.get("/status")
async def status(user=Depends(get_current_user)):
    sub = await get_subscription(user["id"])
    state = await is_active(user["id"])
    return {"subscription": sub, "entitlement": state}


@sub_router.post("/checkout")
async def create_checkout(payload: dict, request: Request, user=Depends(get_current_user)):
    # Admin grandfather is permanent — block them from accidentally subscribing.
    if user.get("role") == "admin":
        raise HTTPException(
            status_code=400,
            detail="Admin accounts have grandfathered access and cannot subscribe.",
        )
    plan_id = payload.get("plan_id")
    origin = payload.get("origin")
    if not origin or not origin.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="valid origin required")
    plan = get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=400, detail=f"unknown plan: {plan_id}")

    host_url = str(request.base_url)
    stripe = _stripe_client(host_url)
    success_url = f"{origin.rstrip('/')}/subscription/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{origin.rstrip('/')}/subscription"

    metadata = {
        "user_id": user["id"],
        "user_email": user.get("email", ""),
        "plan_id": plan.id,
        "duration_months": str(plan.duration_months),
    }

    req = CheckoutSessionRequest(
        amount=plan.amount_usd,
        currency="usd",
        success_url=success_url,
        cancel_url=cancel_url,
        metadata=metadata,
    )
    try:
        session = await stripe.create_checkout_session(req)
    except Exception as e:
        logger.exception("Stripe checkout create failed for user=%s", user["id"])
        raise HTTPException(
            status_code=502,
            detail="Could not start Stripe checkout. Please try again shortly.",
        ) from e

    await record_transaction(
        user_id=user["id"],
        user_email=user.get("email", ""),
        plan_id=plan.id,
        session_id=session.session_id,
        amount_usd=plan.amount_usd,
        metadata=metadata,
    )
    return {
        "checkout_url": session.url,
        "session_id": session.session_id,
        "plan": plan.to_public(),
    }


@sub_router.get("/poll/{session_id}")
async def poll_session(session_id: str, request: Request, user=Depends(get_current_user)):
    db = get_db()
    # Verify the session belongs to this user
    txn = await db.payment_transactions.find_one(
        {"session_id": session_id, "user_id": user["id"]}
    )
    if not txn:
        raise HTTPException(status_code=404, detail="transaction not found")
    if txn.get("payment_status") == "paid" and txn.get("applied"):
        sub = await get_subscription(user["id"])
        return {"payment_status": "paid", "already_applied": True,
                "subscription": sub}

    host_url = str(request.base_url)
    stripe = _stripe_client(host_url)
    try:
        status = await stripe.get_checkout_status(session_id)
    except Exception as e:
        logger.exception("Stripe poll failed for session=%s", session_id)
        raise HTTPException(
            status_code=502,
            detail="Could not verify payment with Stripe right now.",
        ) from e

    if status.payment_status == "paid":
        sub = await apply_successful_payment(session_id)
        return {"payment_status": "paid", "subscription": sub,
                "status": status.status, "amount_total": status.amount_total}
    if status.status == "expired":
        await db.payment_transactions.update_one(
            {"session_id": session_id},
            {"$set": {"payment_status": "expired"}}
        )
        return {"payment_status": "expired", "status": status.status}
    return {"payment_status": status.payment_status, "status": status.status}


@hook_router.post("/stripe")
async def stripe_webhook(request: Request):
    host_url = str(request.base_url)
    stripe = _stripe_client(host_url)
    body = await request.body()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = await stripe.handle_webhook(body, sig)
    except Exception as e:
        logger.exception("Stripe webhook handle failed")
        raise HTTPException(status_code=400, detail="webhook signature invalid") from e
    # Only act on terminal payment events
    if event.payment_status == "paid" and event.session_id:
        await apply_successful_payment(event.session_id)
    logger.info("Stripe webhook event_type=%s session=%s status=%s",
                event.event_type, event.session_id, event.payment_status)
    return {"ok": True}


router.include_router(sub_router)
router.include_router(hook_router)
