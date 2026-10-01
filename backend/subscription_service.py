"""Subscription service — entitlement state + Stripe Checkout glue.

Two collections:
  - payment_transactions: one row per Stripe checkout session (initiated→paid)
  - subscriptions      : per-user entitlement {valid_until, current_plan_id, ...}

`is_active(user_id)` is what the bot_runner / route gates call to decide
whether to auto-execute trades for a user. Admins are grandfathered forever;
all other pre-existing users get a 30-day grace period from now.
"""
from datetime import datetime, timezone, timedelta
import logging
from typing import Optional
from dateutil.relativedelta import relativedelta
from database import get_db

logger = logging.getLogger(__name__)
from subscription_plans import (
    get_plan, get_tier_features, Features, TIER_RANK, TIER_BASE_CENTS,
    canonical_tier,
)


# Existing users created BEFORE this timestamp get a 30-day grace period.
# Stored in db.users.created_at; we treat anything older than rollout cutoff
# (== first time this module loads) as legacy.
LEGACY_GRACE_DAYS = 30
_ROLLOUT_AT = datetime.now(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def trial_grant_for_signup(created: datetime) -> Optional[dict]:
    """audit r28 P2-03 — the durable, versioned trial grant written on the user
    document AT REGISTRATION (immutable tier/start/end + the offer version that
    produced it). Later offer edits never change an existing grant."""
    from plan_settings import trial_config
    cfg = trial_config()
    if cfg["days"] <= 0 or not cfg.get("enabled_at"):
        return None
    enabled_at = datetime.fromisoformat(cfg["enabled_at"].replace("Z", "+00:00"))
    if created < enabled_at:
        return None
    ends = created + timedelta(days=cfg["days"])
    return {"tier": cfg["tier"], "days": cfg["days"], "started_at": created.isoformat(),
            "ends_at": ends.isoformat(), "offer_version": cfg.get("offer_version"),
            "granted_at": _now().isoformat()}


def _trial_grant(created: Optional[datetime], user: Optional[dict] = None) -> Optional[dict]:
    """Entitlement from the user's durable grant (preferred). Users registered
    before durable grants existed fall back to the lazy evaluation once."""
    grant = (user or {}).get("trial_grant")
    if not grant and isinstance(created, datetime):
        grant = trial_grant_for_signup(created)
    if not grant:
        return None
    ends = datetime.fromisoformat(str(grant["ends_at"]).replace("Z", "+00:00"))
    if ends <= _now():
        return None
    return {"current_plan_id": f"trial_{grant['tier']}", "valid_until": ends.isoformat(),
            "trial": {k: grant.get(k) for k in ("tier", "days", "started_at", "ends_at", "offer_version")}}


async def get_user_tier(user_id: str) -> str:
    """Resolve the user's effective subscription tier — returns
    `"admin" | "elite_ai" | "professional" | "trader" | "starter"`
    (legacy plan ids resolve forward: pro→trader, elite→professional).

    Resolution order:
      1. Admin role → "admin" (full bypass).
      2. Active paid subscription → tier from current_plan_id.
      3. In legacy grace period → "trader" (existing paying customers
         pre-tier-rollout get grandfathered until grace expires).
      4. Otherwise → "starter".
    """
    db = get_db()
    sub = await get_subscription(user_id)
    state = await is_active(user_id)
    plan_id = (sub.get("current_plan_id") or "") if sub else ""
    if plan_id == "admin_grandfather":
        return "admin"
    if not state.get("active"):
        return "starter"
    if plan_id.startswith("trial_"):
        return canonical_tier(plan_id[len("trial_"):])
    # In-grace legacy customers get Trader
    if state.get("in_grace"):
        return "trader"
    # Active paid subscription — derive tier from plan_id
    plan = get_plan(plan_id)
    if plan:
        return plan.tier
    return "starter"


async def get_user_features(user_id: str) -> Features:
    """Convenience: resolve the user's tier + return the Features dataclass."""
    return get_tier_features(await get_user_tier(user_id))


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
    # Legacy grace applies ONLY to users who existed before the tier rollout —
    # a brand-new sign-up must start at Starter, not 30 days of free Trader.
    pre_rollout = False
    created = None
    if user and not is_admin:
        created = user.get("created_at")
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                created = None
        if isinstance(created, datetime):
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            pre_rollout = created < _ROLLOUT_AT
    stub = {
        "user_id": user_id,
        "current_plan_id": "admin_grandfather" if is_admin else None,
        "valid_until": (_now() + timedelta(days=365 * 10)).isoformat() if is_admin else None,
        "auto_renew": False,
        "grace_until": (_ROLLOUT_AT + timedelta(days=LEGACY_GRACE_DAYS)).isoformat()
        if pre_rollout else None,
        "created_at": _now().isoformat(),
    }
    trial = _trial_grant(created, user) if (user and not is_admin and not pre_rollout) else None
    if trial:
        stub.update(trial)
    try:
        await db.subscriptions.insert_one({**stub})
    except Exception:  # noqa: BLE001 — concurrent first read lost the unique(user_id) race; the winner's row stands
        pass
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
    # 3) Scheduled (downgraded) pass — lazily promote once the current
    #    pass has lapsed. Downgrades never shorten the paid-for period.
    sched_id = sub.get("scheduled_plan_id")
    sched_vu_raw = sub.get("scheduled_valid_until")
    if sched_id and sched_vu_raw:
        try:
            sched_vu = datetime.fromisoformat(sched_vu_raw.replace("Z", "+00:00"))
        except ValueError:
            sched_vu = None
        if sched_vu and sched_vu > now:
            db = get_db()
            await db.subscriptions.update_one(
                {"user_id": user_id},
                {"$set": {"current_plan_id": sched_id,
                          "valid_until": sched_vu_raw,
                          "scheduled_plan_id": None,
                          "scheduled_valid_until": None}},
            )
            return {"active": True, "reason": "scheduled_plan_started",
                    "valid_until": sched_vu_raw, "in_grace": False,
                    "plan_id": sched_id}
    return {"active": False, "reason": "expired_or_unsubscribed",
            "valid_until": valid_until, "in_grace": False, "plan_id": None}


async def record_transaction(
    *, user_id: str, user_email: str, plan_id: str,
    session_id: str, amount_usd: float, metadata: dict,
    amount_cents: Optional[int] = None, currency: str = "usd",
    pricing_version: Optional[int] = None,
) -> str:
    db = get_db()
    doc = {
        "user_id": user_id,
        "user_email": user_email,
        "plan_id": plan_id,
        "session_id": session_id,
        "amount_usd": amount_usd,
        # audit r28 P2-01 — signed-at-checkout price snapshot; fulfilment must reproduce it
        "amount_minor": int(amount_cents if amount_cents is not None else round(amount_usd * 100)),
        "currency": (currency or "usd").lower(),
        "pricing_version": pricing_version,
        "metadata": metadata,
        "payment_status": "initiated",
        "created_at": _now().isoformat(),
    }
    r = await db.payment_transactions.insert_one(doc)
    return str(r.inserted_id)


_CLAIM_STALE_MINUTES = 5


async def apply_successful_payment(session_id: str, *,
                                   source: str = "unknown",
                                   paid_amount_minor: Optional[int] = None,
                                   paid_currency: Optional[str] = None) -> Optional[dict]:
    """Exactly-once payment application (iter-122).

    The Stripe webhook and the browser poll can both observe `paid`
    concurrently — an atomic single-writer CLAIM on the ledger row
    guarantees only one of them extends the subscription. The claim
    self-heals after _CLAIM_STALE_MINUTES if a holder crashed mid-apply.

    Durations are calendar-aware (relativedelta months — annual means one
    calendar year, not 360 days). Cross-tier purchases prorate:
      • upgrade   — remaining days convert into equal-VALUE days of the new
                    tier immediately (value ratio of tier base prices);
      • downgrade — the purchased pass is SCHEDULED to start when the
                    current pass expires (never shortens what was paid for).
    """
    db = get_db()
    now = _now()
    stale_before = (now - timedelta(minutes=_CLAIM_STALE_MINUTES)).isoformat()
    txn = await db.payment_transactions.find_one_and_update(
        {"session_id": session_id, "applied": {"$ne": True},
         "$or": [{"apply_claimed_at": None},
                 {"apply_claimed_at": {"$exists": False}},
                 {"apply_claimed_at": {"$lt": stale_before}}]},
        {"$set": {"apply_claimed_at": now.isoformat(), "apply_source": source}},
    )
    if not txn:
        return None  # unknown session, already applied, or claim held elsewhere

    # audit r28 P2-01 — refuse fulfilment when Stripe's settled amount/currency does not
    # reproduce the price snapshot recorded at checkout (never grant on a mismatched price).
    snap_minor, snap_cur = txn.get("amount_minor"), str(txn.get("currency") or "usd").lower()
    if paid_amount_minor is not None and snap_minor is not None and (
            int(paid_amount_minor) != int(snap_minor)
            or (paid_currency and str(paid_currency).lower() != snap_cur)):
        logger.error("price snapshot mismatch session=%s paid=%s %s snapshot=%s %s",
                     session_id, paid_amount_minor, paid_currency, snap_minor, snap_cur)
        await db.payment_transactions.update_one(
            {"session_id": session_id},
            {"$set": {"payment_status": "paid", "applied": False, "apply_claimed_at": None,
                      "skipped_reason": "price_mismatch", "fulfilment_blocked": True,
                      "paid_amount_minor": int(paid_amount_minor), "paid_currency": paid_currency}})
        return None

    try:
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
                          "applied_at": now.isoformat(),
                          "skipped_reason": "admin_grandfather"}},
            )
            return await get_subscription(txn["user_id"])

        plan = get_plan(txn["plan_id"])
        if not plan:
            await db.payment_transactions.update_one(
                {"session_id": session_id},
                {"$set": {"payment_status": "paid", "applied": True,
                          "applied_at": now.isoformat(),
                          "skipped_reason": "unknown_plan"}},
            )
            return None

        sub = await get_subscription(txn["user_id"])
        current_vu = None
        if sub.get("valid_until"):
            try:
                current_vu = datetime.fromisoformat(
                    sub["valid_until"].replace("Z", "+00:00"))
            except ValueError:
                current_vu = None
        active_remaining = current_vu is not None and current_vu > now
        cur_plan = get_plan(sub.get("current_plan_id") or "")

        update = {"last_renewed_at": now.isoformat(),
                  "last_session_id": session_id}
        if active_remaining and cur_plan and cur_plan.tier != plan.tier:
            cur_rank = TIER_RANK[canonical_tier(cur_plan.tier)]
            new_rank = TIER_RANK[canonical_tier(plan.tier)]
            if new_rank > cur_rank:
                # UPGRADE — convert remaining time into equal-value new-tier days
                remaining_days = (current_vu - now).total_seconds() / 86400.0
                credit_days = remaining_days * (
                    TIER_BASE_CENTS[cur_plan.tier] / TIER_BASE_CENTS[plan.tier])
                new_vu = (now + relativedelta(months=plan.duration_months)
                          + timedelta(days=credit_days))
                update.update({
                    "current_plan_id": plan.id,
                    "valid_until": new_vu.isoformat(),
                    "scheduled_plan_id": None, "scheduled_valid_until": None,
                    "proration": {"kind": "upgrade", "from_plan": cur_plan.id,
                                  "credited_days": round(credit_days, 2),
                                  "at": now.isoformat()},
                })
            else:
                # DOWNGRADE — schedule the new pass after the current one ends
                sched_vu = current_vu + relativedelta(months=plan.duration_months)
                new_vu = current_vu
                update.update({
                    "scheduled_plan_id": plan.id,
                    "scheduled_valid_until": sched_vu.isoformat(),
                    "proration": {"kind": "downgrade_scheduled",
                                  "from_plan": cur_plan.id,
                                  "starts_at": current_vu.isoformat(),
                                  "at": now.isoformat()},
                })
        else:
            # Same tier (or nothing active) — extend from the later of
            # valid_until / now, calendar-aware.
            base = current_vu if active_remaining else now
            new_vu = base + relativedelta(months=plan.duration_months)
            update.update({"current_plan_id": plan.id,
                           "valid_until": new_vu.isoformat()})

        await db.subscriptions.update_one(
            {"user_id": txn["user_id"]}, {"$set": update}, upsert=True)
        await db.payment_transactions.update_one(
            {"session_id": session_id},
            {"$set": {"payment_status": "paid", "applied": True,
                      "applied_at": now.isoformat(),
                      "new_valid_until": new_vu.isoformat()}},
        )
    except Exception:
        # Release the claim so a later webhook/poll retry can re-apply.
        await db.payment_transactions.update_one(
            {"session_id": session_id, "applied": {"$ne": True}},
            {"$set": {"apply_claimed_at": None}})
        raise

    # Affiliate commission — durable OUTBOX (never blocks payment). The
    # billing loop retries failed entries; commission creation itself is
    # idempotent (unique (session_id, tier) index).
    try:
        await db.affiliate_outbox.update_one(
            {"session_id": session_id},
            {"$setOnInsert": {
                "session_id": session_id,
                "user_id": txn["user_id"],
                "plan_id": plan.id,
                "amount_usd": float(txn.get("amount_usd") or plan.amount_usd),
                "status": "pending", "attempts": 0,
                "created_at": now.isoformat(),
            }},
            upsert=True,
        )
        from affiliate_service import process_affiliate_outbox
        await process_affiliate_outbox(db, only_session=session_id)
    except Exception:
        pass  # the billing loop will retry pending outbox entries

    out = await db.subscriptions.find_one({"user_id": txn["user_id"]})
    out["id"] = str(out.pop("_id"))
    return out


async def revoke_payment(session_id: str, *, reason: str = "refund") -> Optional[dict]:
    """Refund / chargeback handling: pull the purchased duration back out of
    `valid_until` (or drop a scheduled pass) and reverse any affiliate
    commissions for the session. Idempotent via the `revoked` flag."""
    db = get_db()
    now = _now()
    txn = await db.payment_transactions.find_one_and_update(
        {"session_id": session_id, "applied": True, "revoked": {"$ne": True}},
        {"$set": {"revoked": True, "revoked_at": now.isoformat(),
                  "revoke_reason": reason, "payment_status": "refunded"}},
    )
    if not txn:
        return None
    plan = get_plan(txn.get("plan_id") or "")
    sub = await get_subscription(txn["user_id"])
    if plan and not txn.get("skipped_reason"):
        if sub.get("scheduled_plan_id") == plan.id:
            await db.subscriptions.update_one(
                {"user_id": txn["user_id"]},
                {"$set": {"scheduled_plan_id": None,
                          "scheduled_valid_until": None}})
        elif sub.get("valid_until"):
            try:
                vu = datetime.fromisoformat(
                    sub["valid_until"].replace("Z", "+00:00"))
                new_vu = vu - relativedelta(months=plan.duration_months)
            except ValueError:
                new_vu = now
            await db.subscriptions.update_one(
                {"user_id": txn["user_id"]},
                {"$set": {"valid_until": new_vu.isoformat(),
                          "last_revoked_session": session_id}})
    try:
        from affiliate_service import reverse_commissions_for_session
        await reverse_commissions_for_session(db, session_id, reason=reason)
    except Exception:
        pass
    return await get_subscription(txn["user_id"])
