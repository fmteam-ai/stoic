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
    revoke_payment,
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


@sub_router.get("/transactions")
async def list_transactions(user=Depends(get_current_user), limit: int = 50):
    """Return the current user's payment-transaction history, newest first.

    Only returns the fields safe to surface in the UI — no raw Stripe metadata.
    """
    from database import get_db
    db = get_db()
    cursor = db.payment_transactions.find(
        {"user_id": user["id"]}
    ).sort("created_at", -1).limit(max(1, min(limit, 200)))
    out = []
    async for tx in cursor:
        out.append({
            "id": str(tx.get("_id") or ""),
            "plan_id": tx.get("plan_id"),
            "amount_usd": float(tx.get("amount_usd") or 0),
            "currency": (tx.get("currency") or "usd").upper(),
            "payment_status": tx.get("payment_status") or "unknown",
            "session_id": tx.get("session_id"),
            "created_at": tx.get("created_at"),
            "completed_at": tx.get("completed_at"),
        })
    return out



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
    # iter-122: checkout redirect origins come from a configured allowlist —
    # never trust an arbitrary client-supplied URL (open-redirect vector).
    allowed = {
        o.strip().rstrip("/")
        for o in os.environ.get("CHECKOUT_ALLOWED_ORIGINS", "").split(",")
        if o.strip()
    }
    if not origin or origin.rstrip("/") not in allowed:
        raise HTTPException(status_code=400,
                            detail="origin not in the approved domain list")
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
        sub = (await apply_successful_payment(session_id, source="poll")
               or await get_subscription(user["id"]))
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
        # All malformed-webhook cases (bad sig, missing fields, library
        # wrapping a KeyError into CheckoutError) end here. Synthetic test
        # calls hit this path constantly — log at WARNING without stack-trace
        # so err.log stays clean for real issues.
        logger.warning("Stripe webhook rejected: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=400, detail="webhook signature invalid") from e
    # Only act on terminal payment events
    et = (getattr(event, "event_type", "") or "").lower()
    if event.payment_status == "paid" and event.session_id:
        await apply_successful_payment(event.session_id, source="webhook")
    elif event.session_id and ("refund" in et or "dispute" in et
                               or "charge_failed" in et):
        await revoke_payment(event.session_id, reason=et or "refund")
    logger.info("Stripe webhook event_type=%s session=%s status=%s",
                event.event_type, event.session_id, event.payment_status)
    return {"ok": True}


@sub_router.post("/admin/refund")
async def admin_refund(payload: dict, user=Depends(get_current_user)):
    """Manual refund/chargeback processing (admin): revokes the purchased
    access period and reverses affiliate commissions for the session.
    (The Stripe money movement itself happens in the Stripe dashboard.)"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    sub = await revoke_payment(session_id,
                               reason=payload.get("reason") or "manual_refund")
    if sub is None:
        raise HTTPException(
            status_code=404,
            detail="transaction not found, not applied, or already revoked")
    return {"ok": True, "subscription": sub}


router.include_router(sub_router)
router.include_router(hook_router)
