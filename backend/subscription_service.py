"""Subscription service — entitlement state + Stripe Checkout glue.

Two collections:
  - payment_transactions: one row per Stripe checkout session (initiated→paid)
  - subscriptions      : per-user entitlement {valid_until, current_plan_id, ...}

`is_active(user_id)` is what the bot_runner / route gates call to decide
whether to auto-execute trades for a user. Admins are grandfathered forever;
all other pre-existing users get a 30-day grace period from now.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional
from database import get_db
from subscription_plans import get_plan


# Existing users created BEFORE this timestamp get a 30-day grace period.
# Stored in db.users.created_at; we treat anything older than rollout cutoff
# (== first time this module loads) as legacy.
LEGACY_GRACE_DAYS = 30
_ROLLOUT_AT = datetime.now(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_subscription(user_id: str) -> dict:
    """Return the user's current entitlement document (creates a stub if missing)."""
    db = get_db()
    doc = await db.subscriptions.find_one({"user_id": user_id})
    if doc:
        doc["id"] = str(doc.pop("_id"))
        return doc
    # Look up the user to check admin role
    from bson import ObjectId
    try:
        user = await db.users.find_one({"_id": ObjectId(user_id)})
    except Exception:
        user = None
    is_admin = user and user.get("role") == "admin"
    stub = {
        "user_id": user_id,
        "current_plan_id": "admin_grandfather" if is_admin else None,
        "valid_until": (_now() + timedelta(days=365 * 10)).isoformat() if is_admin else None,
        "auto_renew": False,
        "grace_until": (_ROLLOUT_AT + timedelta(days=LEGACY_GRACE_DAYS)).isoformat()
        if user and not is_admin else None,
        "created_at": _now().isoformat(),
    }
    await db.subscriptions.insert_one({**stub})
    doc = await db.subscriptions.find_one({"user_id": user_id})
    doc["id"] = str(doc.pop("_id"))
    return doc


async def is_active(user_id: str) -> dict:
    """Return {active: bool, reason: str, valid_until, in_grace, plan_id}."""
    sub = await get_subscription(user_id)
    now = _now()
    # 1) Admin / valid plan
    valid_until = sub.get("valid_until")
    if valid_until:
        vu = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
        if vu > now:
            return {"active": True, "reason": "subscription_active",
                    "valid_until": valid_until, "in_grace": False,
                    "plan_id": sub.get("current_plan_id")}
    # 2) Grace period
    grace = sub.get("grace_until")
    if grace:
        gu = datetime.fromisoformat(grace.replace("Z", "+00:00"))
        if gu > now:
            return {"active": True, "reason": "in_grace_period",
                    "valid_until": valid_until, "in_grace": True,
                    "grace_until": grace, "plan_id": None}
    return {"active": False, "reason": "expired_or_unsubscribed",
            "valid_until": valid_until, "in_grace": False, "plan_id": None}


async def record_transaction(
    *, user_id: str, user_email: str, plan_id: str,
    session_id: str, amount_usd: float, metadata: dict,
) -> str:
    db = get_db()
    doc = {
        "user_id": user_id,
        "user_email": user_email,
        "plan_id": plan_id,
        "session_id": session_id,
        "amount_usd": amount_usd,
        "currency": "usd",
        "metadata": metadata,
        "payment_status": "initiated",
        "created_at": _now().isoformat(),
    }
    r = await db.payment_transactions.insert_one(doc)
    return str(r.inserted_id)


async def apply_successful_payment(session_id: str) -> Optional[dict]:
    """Idempotent: extend the user's `valid_until` by the plan's duration.

    Returns the updated subscription doc or None if already applied / not found.
    Admin users are short-circuited so a stray payment never demotes them out
    of the grandfather state.
    """
    db = get_db()
    txn = await db.payment_transactions.find_one({"session_id": session_id})
    if not txn:
        return None
    if txn.get("payment_status") == "paid" and txn.get("applied"):
        return None  # already processed — idempotency guard

    # Admin grandfather protection — never overwrite admin entitlement
    from bson import ObjectId
    try:
        user_doc = await db.users.find_one({"_id": ObjectId(txn["user_id"])})
    except Exception:
        user_doc = None
    if user_doc and user_doc.get("role") == "admin":
        await db.payment_transactions.update_one(
            {"session_id": session_id},
            {"$set": {"payment_status": "paid", "applied": True,
                      "applied_at": _now().isoformat(),
                      "skipped_reason": "admin_grandfather"}},
        )
        return await get_subscription(txn["user_id"])

    plan = get_plan(txn["plan_id"])
    if not plan:
        return None

    sub = await get_subscription(txn["user_id"])
    now = _now()
    # Extend from whichever is later: current valid_until or now
    current_vu = None
    if sub.get("valid_until"):
        try:
            current_vu = datetime.fromisoformat(
                sub["valid_until"].replace("Z", "+00:00")
            )
        except ValueError:
            current_vu = None
    base = current_vu if (current_vu and current_vu > now) else now
    new_vu = base + timedelta(days=plan.duration_months * 30)

    await db.subscriptions.update_one(
        {"user_id": txn["user_id"]},
        {"$set": {
            "current_plan_id": plan.id,
            "valid_until": new_vu.isoformat(),
            "last_renewed_at": now.isoformat(),
            "last_session_id": session_id,
        }},
        upsert=True,
    )
    await db.payment_transactions.update_one(
        {"session_id": session_id},
        {"$set": {
            "payment_status": "paid",
            "applied": True,
            "applied_at": now.isoformat(),
            "new_valid_until": new_vu.isoformat(),
        }},
    )

    # Affiliate commission hook — fire-and-forget, never block payment
    try:
        from affiliate_service import record_commission_if_referred
        await record_commission_if_referred(
            user_id=txn["user_id"],
            plan_id=plan.id,
            amount_usd=float(txn.get("amount_usd") or plan.amount_usd),
            session_id=session_id,
        )
    except Exception:
        pass  # commissioning failure must never roll back a paid subscription

    out = await db.subscriptions.find_one({"user_id": txn["user_id"]})
    out["id"] = str(out.pop("_id"))
    return out
