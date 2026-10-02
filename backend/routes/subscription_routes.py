"""Subscription + Stripe Checkout routes.

POST /api/subscription/checkout    — create Stripe Checkout session for a plan
GET  /api/subscription/status      — current entitlement
GET  /api/subscription/plans       — public catalog
GET  /api/subscription/poll/{sid}  — poll Stripe status, sync our DB
POST /api/webhook/stripe           — Stripe webhook receiver
"""
import os
import asyncio
import json as _json
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from emergentintegrations.payments.stripe.checkout import (
    StripeCheckout, CheckoutSessionRequest,
)

from auth import get_current_user
from subscription_plans import get_plan, all_plans_public
from subscription_service import (
    get_subscription, is_active, apply_successful_payment,
    revoke_payment, fulfil_paid_session, is_pending_session_id,
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
    # When a signing secret is configured (production / claimed sandbox),
    # emergentintegrations verifies the Stripe-Signature via
    # stripe.Webhook.construct_event. In the shared preview sandbox no secret
    # exists, so we ALSO re-verify every grant against Stripe directly
    # (see stripe_webhook) — forged events can never fabricate a paid status.
    return StripeCheckout(
        api_key=key,
        webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET") or None,
        webhook_url=webhook_url,
    )


def _sp_version() -> int:
    import subscription_plans as _sp
    return _sp.PRICING_VERSION


@sub_router.get("/plans")
async def list_plans():
    import plan_settings
    await plan_settings.ensure_fresh(get_db())
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
            "session_id": None if is_pending_session_id(tx.get("session_id")) else tx.get("session_id"),
            "created_at": tx.get("created_at"),
            "completed_at": tx.get("completed_at"),
        })
    return out



@sub_router.get("/upgrade-preview")
async def upgrade_preview(user=Depends(get_current_user)):
    """Per-plan proration preview: what happens to the user's remaining
    prepaid time if they buy each plan right now. Mirrors the logic in
    subscription_service.apply_successful_payment."""
    from datetime import datetime, timezone, timedelta
    from dateutil.relativedelta import relativedelta
    from subscription_plans import (
        all_plans_public, TIER_RANK, TIER_BASE_CENTS, canonical_tier,
    )
    from subscription_service import get_subscription

    sub = await get_subscription(user["id"])
    now = datetime.now(timezone.utc)
    current_vu = None
    if sub.get("valid_until"):
        try:
            current_vu = datetime.fromisoformat(
                sub["valid_until"].replace("Z", "+00:00"))
        except ValueError:
            current_vu = None
    active_remaining = current_vu is not None and current_vu > now
    cur_plan = get_plan(sub.get("current_plan_id") or "")

    previews = {}
    for p in all_plans_public():
        plan = get_plan(p["id"])
        if not plan:
            continue
        if not active_remaining or not cur_plan:
            new_vu = now + relativedelta(months=plan.duration_months)
            previews[plan.id] = {"kind": "new",
                                 "new_valid_until": new_vu.isoformat()}
            continue
        if cur_plan.tier == plan.tier:
            new_vu = current_vu + relativedelta(months=plan.duration_months)
            previews[plan.id] = {"kind": "extend",
                                 "new_valid_until": new_vu.isoformat()}
            continue
        cur_rank = TIER_RANK[canonical_tier(cur_plan.tier)]
        new_rank = TIER_RANK[canonical_tier(plan.tier)]
        if new_rank > cur_rank:
            remaining_days = (current_vu - now).total_seconds() / 86400.0
            credit_days = remaining_days * (
                TIER_BASE_CENTS[cur_plan.tier] / TIER_BASE_CENTS[plan.tier])
            new_vu = (now + relativedelta(months=plan.duration_months)
                      + timedelta(days=credit_days))
            previews[plan.id] = {"kind": "upgrade",
                                 "credited_days": round(credit_days, 1),
                                 "new_valid_until": new_vu.isoformat()}
        else:
            sched_vu = current_vu + relativedelta(months=plan.duration_months)
            previews[plan.id] = {"kind": "downgrade_scheduled",
                                 "starts_at": current_vu.isoformat(),
                                 "new_valid_until": sched_vu.isoformat()}
    return {"active": active_remaining,
            "current_plan_id": sub.get("current_plan_id"),
            "valid_until": sub.get("valid_until"),
            "previews": previews}


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
    import plan_settings
    await plan_settings.ensure_fresh(get_db())          # audit r28 P2-01 — quote from the shared snapshot
    plan = get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=400, detail=f"unknown plan: {plan_id}")
    # audit r29 P1-01 — single immutable snapshot; nothing below re-reads pricing globals
    import subscription_plans as _sp
    import subscription_service as _ss
    # audit v2 billing — deterministic idempotency key per checkout intent
    client_key = request.headers.get("Idempotency-Key") if hasattr(request, "headers") else None
    host_url = str(request.base_url)
    success_url = f"{origin.rstrip('/')}/subscription/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{origin.rstrip('/')}/subscription"

    row = snap = None
    for attempt in range(5):
        try:
            key = _sp.checkout_idempotency_key(user["id"], plan, _sp.PRICING_VERSION,
                                               client_key=client_key, attempt=attempt)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        snap = _sp.pricing_snapshot(plan, idempotency_key=key)
        metadata = {
            "user_id": user["id"],
            "user_email": user.get("email", ""),
            "plan_id": snap["plan_id"],
            "duration_months": str(snap["duration_months"]),
            "amount_minor": str(snap["amount_minor"]),
            "currency": snap["currency"],
            "pricing_version": str(snap["pricing_version"]),
            "idempotency_key": key,
        }
        # 1) ledger row FIRST (status "initiated", unique by idempotency_key)
        row, _created = await _ss.open_checkout_intent(
            idempotency_key=key, user_id=user["id"], user_email=user.get("email", ""),
            plan_id=snap["plan_id"], amount_usd=snap["amount_usd"], metadata=metadata,
            amount_cents=snap["amount_minor"], currency=snap["currency"],
            pricing_version=snap["pricing_version"], snapshot=snap)
        if row.get("user_id") != user["id"] or row.get("plan_id") != snap["plan_id"] \
                or row.get("pricing_version") != snap["pricing_version"]:
            raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different checkout")
        terminal = row.get("applied") or row.get("payment_status") in _ss.TERMINAL_PAYMENT_STATUSES
        if terminal and client_key is None:
            continue            # previous intent in this window is finished — derive a fresh one
        break
    else:
        raise HTTPException(status_code=429, detail="Too many checkouts for this plan; try again shortly.")

    def _response(r: dict, reused: bool) -> dict:
        return {"checkout_url": r.get("checkout_url"), "session_id": r.get("session_id"),
                "plan": snap["public"], "pricing_version": snap["pricing_version"],
                "idempotency_key": key, "reused": reused}

    # 2) a retry of an intent that already has a Stripe session reuses it
    if not _ss.is_pending_session_id(row.get("session_id")):
        return _response(row, True)

    # 3) create the Stripe session. The emergentintegrations wrapper's
    #    create_checkout_session(request) takes no idempotency key; when a wrapper
    #    exposes one (`idempotency_key` kwarg) we pass ours so a crash between Stripe
    #    create and step 4 replays the SAME session. Without it, the row-first +
    #    reuse logic here and the webhook's metadata.idempotency_key recovery
    #    (fulfil_paid_session) are the guarantee.
    stripe = _stripe_client(host_url)
    req = CheckoutSessionRequest(
        amount=row.get("amount_usd", snap["amount_usd"]),
        currency=row.get("currency", snap["currency"]),
        success_url=success_url,
        cancel_url=cancel_url,
        metadata=dict(row.get("metadata") or metadata),
    )
    await _ss.mark_checkout_create_attempt(key)
    try:
        if _accepts_idempotency_key(stripe.create_checkout_session):
            session = await stripe.create_checkout_session(req, idempotency_key=key)
        else:
            session = await stripe.create_checkout_session(req)
    except Exception as e:
        logger.exception("Stripe checkout create failed for user=%s", user["id"])
        raise HTTPException(
            status_code=502,
            detail="Could not start Stripe checkout. Please try again shortly.",
        ) from e

    # 4) bind the session to the intent row (a concurrent twin may have won)
    row = await _ss.attach_checkout_session(key, session.session_id, session.url)
    return _response(row, row.get("session_id") != session.session_id)


def _accepts_idempotency_key(fn) -> bool:
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "idempotency_key" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


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
        sub = (await apply_successful_payment(session_id, source="poll",
                                              paid_amount_minor=getattr(status, "amount_total", None),
                                              paid_currency=getattr(status, "currency", None))
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

async def _resolve_session_id_for_revoke(body: bytes) -> str | None:
    """Charge-level refund/dispute events (charge.refunded,
    charge.dispute.created, refund.created) carry a payment_intent, not a
    Checkout session id — the emergentintegrations parser leaves session_id
    None for them. Map back to the originating Checkout session via the
    payment_intent so revoke_payment can pull the entitlement/commission."""
    try:
        obj = (_json.loads(body).get("data", {}) or {}).get("object", {}) or {}
    except Exception:  # noqa: BLE001
        return None
    pi = obj.get("payment_intent")
    if not pi:
        return None

    def _lookup():
        import stripe as stripe_sdk
        stripe_sdk.api_key = os.environ["STRIPE_API_KEY"]
        sessions = stripe_sdk.checkout.Session.list(payment_intent=pi, limit=1)
        return sessions.data[0].id if sessions.data else None

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _lookup)
    except Exception as e:  # noqa: BLE001
        logger.warning("revoke: session lookup failed (pi=%s): %s", pi, e)
        return None



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
    try:  # iter-137 — ops console webhook feed (observability only)
        from datetime import datetime, timezone
        await get_db().stripe_webhook_events.insert_one(
            {"type": et or "unknown",
             "at": datetime.now(timezone.utc).isoformat()})
    except Exception:  # noqa: BLE001
        pass
    signature_verified = bool(os.environ.get("STRIPE_WEBHOOK_SECRET"))
    if event.payment_status == "paid" and event.session_id:
        # SEC — NEVER trust the webhook body for granting paid entitlement.
        # Independently re-confirm with Stripe (works even when no signing
        # secret is configured), so a forged/unsigned event cannot fabricate
        # a paid subscription or affiliate commission.
        try:
            status = await stripe.get_checkout_status(event.session_id)
        except Exception as e:
            logger.warning("Stripe re-verify failed for session=%s: %s: %s",
                           event.session_id, type(e).__name__, e)
            raise HTTPException(
                status_code=502,
                detail="Could not verify payment with Stripe.") from e
        if status.payment_status == "paid":
            # audit v2 — never drop a paid session silently: unknown sessions are
            # recovered via metadata (idempotency_key → intent row, else rebuild) or
            # recorded in orphan_payments + ops alert. Metadata is taken from the
            # Stripe re-verified session; the event body's copy only when signed.
            md = getattr(status, "metadata", None) or (
                getattr(event, "metadata", None) if signature_verified else None) or {}
            result = await fulfil_paid_session(
                event.session_id, metadata=dict(md), source="webhook",
                paid_amount_minor=getattr(status, "amount_total", None),
                paid_currency=getattr(status, "currency", None))
            logger.info("webhook fulfilment session=%s outcome=%s", event.session_id, result.get("outcome"))
        else:
            logger.warning("Webhook 'paid' for session=%s rejected — Stripe "
                           "reports payment_status=%s (possible forgery)",
                           event.session_id, status.payment_status)
            raise HTTPException(status_code=400,
                                detail="payment not confirmed by Stripe")
    elif ("refund" in et or "dispute" in et or "charge_failed" in et):
        # Revocation is honored ONLY when the signature was cryptographically
        # verified — a forged refund/dispute could otherwise grief a paying
        # user. Charge-level events carry no Checkout session id, so resolve
        # it from the payment_intent before revoking.
        if not signature_verified:
            logger.warning("Ignoring UNSIGNED revoke webhook (%s) for "
                           "session=%s — no STRIPE_WEBHOOK_SECRET", et,
                           event.session_id)
        else:
            sid = event.session_id or await _resolve_session_id_for_revoke(body)
            if sid:
                result = await revoke_payment(sid, reason=et or "refund")
                if result is None:
                    logger.info("revoke webhook %s: no applied txn for "
                                "session=%s (already revoked or unknown)",
                                et, sid)
            else:
                logger.warning("revoke webhook %s: could not resolve a "
                               "Checkout session — no action taken.", et)
    logger.info("Stripe webhook event_type=%s session=%s status=%s",
                event.event_type, event.session_id, event.payment_status)
    return {"ok": True}


@sub_router.post("/admin/refund")
async def admin_refund(payload: dict, user=Depends(get_current_user)):
    """Manual refund/chargeback processing (admin): revokes the purchased
    access period and reverses affiliate commissions for the session.
    (The Stripe money movement itself happens in the Stripe dashboard.)"""
    from auth import require_admin
    require_admin(user)
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
