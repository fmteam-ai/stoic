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


def _signup_offer_version() -> Optional[int]:
    """The offer/pricing version in effect in THIS process right now (the last
    snapshot applied by plan_settings). 0 means no snapshot was ever loaded —
    treated as unknown (None), never as a real version."""
    try:
        import plan_settings
        v = plan_settings.trial_config().get("offer_version")
        v = int(v) if v is not None else None
    except Exception:  # noqa: BLE001
        return None
    return v if v and v > 0 else None


async def decide_trial_at_signup(db, created: datetime) -> dict:
    """audit r29 P2-02 — immutable decision record. A transient failure yields
    `pending_error` carrying the offer version in effect at sign-up (the
    in-process pricing snapshot; None only if none was ever loaded) so a later
    retry can verify it is still deciding against that SAME offer — never
    silently no-grant, never grant a later offer."""
    import plan_settings
    at = _now().isoformat()
    try:
        await plan_settings.ensure_fresh(db)
        offer_version = plan_settings.trial_config().get("offer_version")
        grant = trial_grant_for_signup(created)
        if grant:
            return {"status": "granted", "offer_version": offer_version, "grant": grant, "decided_at": at}
        return {"status": "not_eligible", "offer_version": offer_version, "decided_at": at}
    except Exception as e:  # noqa: BLE001
        logger.exception("trial decision failed at registration")
        return {"status": "pending_error", "offer_version": _signup_offer_version(), "error": str(e)[:200],
                "created_at": created.isoformat(), "decided_at": at, "attempts": 1}


async def retry_pending_trial_decision(db, user: dict) -> Optional[dict]:
    """Idempotent retry of a `pending_error` decision before the first entitlement
    calculation (audit v2). Grants ONLY when the current offer version equals the
    `offer_version` recorded at sign-up. If the recorded version is missing
    (legacy rows → "offer_version_unknown") or the offer has moved since
    ("offer_changed_since_signup"), nothing is granted: the decision stays
    `pending_error` with `admin_review_required=True` + `review_reason`, and the
    user document gets an admin-visible `trial_admin_review` flag (plus a
    best-effort ops alert) so an admin decides explicitly."""
    import plan_settings
    dec = (user or {}).get("trial_decision") or {}
    if dec.get("status") != "pending_error":
        return None
    try:
        created = datetime.fromisoformat(str(dec.get("created_at") or user.get("created_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    attempts = int(dec.get("attempts") or 1) + 1
    review_reason = None
    try:
        await plan_settings.ensure_fresh(db)
        pinned = dec.get("offer_version")
        cur = plan_settings.trial_config()
        if pinned is None:
            review_reason = "offer_version_unknown"
        elif cur.get("offer_version") != pinned:
            review_reason = "offer_changed_since_signup"
        if review_reason:
            # only an explicit, audited admin decision may resolve this — never auto-grant
            new = {**dec, "attempts": attempts, "admin_review_required": True,
                   "review_reason": review_reason,
                   "current_offer_version": cur.get("offer_version"),
                   "note": (f"offer moved {pinned}->{cur.get('offer_version')}; awaiting admin decision"
                            if pinned is not None else
                            "offer version at sign-up unknown; awaiting admin decision")}
        else:
            grant = trial_grant_for_signup(created)
            new = ({"status": "granted", "offer_version": pinned, "grant": grant}
                   if grant else {"status": "not_eligible", "offer_version": pinned})
            new.update({"decided_at": _now().isoformat(), "retried_from": "pending_error",
                        "attempts": attempts})
    except Exception as e:  # noqa: BLE001
        new = {**dec, "attempts": attempts, "error": str(e)[:200]}
    # idempotent: only replace while the stored decision is still the pending one we read
    upd = {"$set": {"trial_decision": new}}
    if new.get("status") == "granted":
        upd["$set"]["trial_grant"] = new["grant"]
    if review_reason:
        upd["$set"]["trial_admin_review"] = {"required": True, "reason": review_reason,
                                             "signup_offer_version": dec.get("offer_version"),
                                             "current_offer_version": new.get("current_offer_version"),
                                             "flagged_at": _now().isoformat()}
    res = await db.users.update_one({"_id": user["_id"], "trial_decision.status": "pending_error",
                                     "trial_decision.decided_at": dec.get("decided_at")}, upd)
    if review_reason and res.modified_count:
        try:
            from alerting import raise_alert
            await raise_alert(db, "trial_decision_review", "warning",
                              f"Trial decision for user {user['_id']} needs an admin decision ({review_reason})",
                              dedup_key=f"trial_decision_review:{user['_id']}",
                              meta={"user_id": str(user["_id"]), "reason": review_reason})
        except Exception:  # noqa: BLE001 — the user-doc flag is the durable signal
            logger.warning("trial review alert failed for user=%s", user.get("_id"))
    return new if res.modified_count else None


def _trial_grant(created: Optional[datetime], user: Optional[dict] = None) -> Optional[dict]:
    """Entitlement from the user's durable grant (preferred). Users registered
    before durable decisions existed fall back to the lazy evaluation once;
    users with an explicit decision never do."""
    grant = (user or {}).get("trial_grant")
    if not grant and (user or {}).get("trial_decision"):
        return None            # explicit not_eligible / still pending — never re-evaluate against a newer offer
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
        if user and (user.get("trial_decision") or {}).get("status") == "pending_error":
            if await retry_pending_trial_decision(db, user):          # r29 P2-02: retry BEFORE first entitlement
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
    pricing_version: Optional[int] = None, snapshot: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> str:
    db = get_db()
    doc = {
        # audit r29 P1-01/P2-03 — the immutable checkout snapshot fulfilment must use exclusively
        "snapshot": {k: v for k, v in (snapshot or {}).items() if k != "public"},
        "idempotency_key": (snapshot or {}).get("idempotency_key"),
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
    if extra:
        doc.update(extra)
    r = await db.payment_transactions.insert_one(doc)
    return str(r.inserted_id)


# ─── audit v2 billing: row-first checkout + crash recovery ───────────────────
# The ledger row is written BEFORE the Stripe session exists. Until the session
# id is known it carries a unique placeholder session_id so it coexists with the
# existing unique(session_id) index (which is not sparse).
PENDING_SESSION_PREFIX = "pending:"
TERMINAL_PAYMENT_STATUSES = ("paid", "expired", "refunded")


def is_pending_session_id(session_id: Optional[str]) -> bool:
    return not session_id or str(session_id).startswith(PENDING_SESSION_PREFIX)


async def open_checkout_intent(*, idempotency_key: str, user_id: str, user_email: str,
                               plan_id: str, amount_usd: float, metadata: dict,
                               amount_cents: int, currency: str, pricing_version,
                               snapshot: dict) -> tuple[dict, bool]:
    """Find-or-create the `payment_transactions` row for one checkout intent
    (keyed by idempotency_key). Returns (row, created). Never touches Stripe."""
    db = get_db()
    row = await db.payment_transactions.find_one({"idempotency_key": idempotency_key})
    if row:
        return row, False
    try:
        await record_transaction(
            user_id=user_id, user_email=user_email, plan_id=plan_id,
            session_id=PENDING_SESSION_PREFIX + idempotency_key, amount_usd=amount_usd,
            metadata=metadata, amount_cents=amount_cents, currency=currency,
            pricing_version=pricing_version, snapshot={**snapshot, "idempotency_key": idempotency_key},
            extra={"checkout_url": None, "stripe_create_attempts": 0})
    except Exception as e:  # noqa: BLE001 — concurrent twin won the unique insert
        row = await db.payment_transactions.find_one({"idempotency_key": idempotency_key})
        if row:
            return row, False
        raise e
    row = await db.payment_transactions.find_one({"idempotency_key": idempotency_key})
    return row, True


async def mark_checkout_create_attempt(idempotency_key: str) -> None:
    await get_db().payment_transactions.update_one(
        {"idempotency_key": idempotency_key},
        {"$inc": {"stripe_create_attempts": 1},
         "$set": {"stripe_create_started_at": _now().isoformat()}})


async def attach_checkout_session(idempotency_key: str, session_id: str, url: Optional[str]) -> dict:
    """Bind the Stripe session to the intent row. Only a still-pending row is
    rebound; if a twin request already attached a session, that one wins and is
    returned (the caller must answer with the winner's session)."""
    db = get_db()
    await db.payment_transactions.update_one(
        {"idempotency_key": idempotency_key,
         "session_id": {"$regex": "^" + PENDING_SESSION_PREFIX}},
        {"$set": {"session_id": session_id, "checkout_url": url,
                  "session_attached_at": _now().isoformat()}})
    return await db.payment_transactions.find_one({"idempotency_key": idempotency_key})


async def _record_orphan_payment(db, session_id: str, *, reason: str, metadata: dict,
                                 paid_amount_minor, paid_currency, source: str) -> dict:
    now = _now().isoformat()
    await db.orphan_payments.update_one(
        {"session_id": session_id},
        {"$setOnInsert": {"session_id": session_id, "reason": reason,
                          "metadata": dict(metadata or {}), "paid_amount_minor": paid_amount_minor,
                          "paid_currency": paid_currency, "source": source,
                          "status": "open", "created_at": now},
         "$set": {"last_seen_at": now}},
        upsert=True)
    try:
        from alerting import raise_alert
        await raise_alert(db, "orphan_payment", "critical",
                          f"PAID Stripe session {session_id} has no fulfilable ledger row ({reason}) — manual reconciliation required",
                          dedup_key=f"orphan_payment:{session_id}",
                          meta={"session_id": session_id, "reason": reason,
                                "user_id": (metadata or {}).get("user_id"),
                                "plan_id": (metadata or {}).get("plan_id"),
                                "idempotency_key": (metadata or {}).get("idempotency_key")})
    except Exception:  # noqa: BLE001 — the orphan_payments row is the durable record
        logger.exception("orphan payment alert failed for session=%s", session_id)
    logger.error("ORPHAN PAYMENT session=%s reason=%s", session_id, reason)
    return {"outcome": "orphaned", "reason": reason, "subscription": None}


def _int_or_none(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def fulfil_paid_session(session_id: str, *, metadata: Optional[dict] = None,
                              source: str = "webhook", paid_amount_minor: Optional[int] = None,
                              paid_currency: Optional[str] = None) -> dict:
    """audit v2 billing — fulfil a session Stripe CONFIRMED as paid, recovering
    when the local row is missing (crash between Stripe create and DB write):

      1. row by session_id            → apply_successful_payment (atomic claim)
      2. row by metadata.idempotency_key (the intent row, possibly still holding
         a placeholder or a superseded unpaid session) → rebind it to this
         session and apply. If that intent was already fulfilled by ANOTHER
         session this is a duplicate payment → orphan + alert.
      3. metadata carries user_id + a known plan for an existing user → rebuild
         the row from metadata (upsert on unique session_id) and apply once.
      4. otherwise → `orphan_payments` record + critical ops alert.
    Never returns silently: the outcome is always one of applied / not_applied
    / rebound / rebuilt / orphaned.
    `metadata` MUST come from Stripe's re-verified session, not an unsigned body."""
    db = get_db()
    md = dict(metadata or {})
    kw = {"source": source, "paid_amount_minor": paid_amount_minor, "paid_currency": paid_currency}
    if await db.payment_transactions.find_one({"session_id": session_id}, {"_id": 1}):
        sub = await apply_successful_payment(session_id, **kw)
        return {"outcome": "applied" if sub else "not_applied", "subscription": sub}   # not_applied: already applied, claim held elsewhere, or price-blocked (row records why)

    key = md.get("idempotency_key")
    if key:
        row = await db.payment_transactions.find_one({"idempotency_key": key})
        if row:
            if row.get("applied") or row.get("payment_status") == "paid":
                return await _record_orphan_payment(
                    db, session_id, reason="duplicate_payment_for_intent", metadata=md, source=source,
                    paid_amount_minor=paid_amount_minor, paid_currency=paid_currency)
            res = await db.payment_transactions.update_one(
                {"_id": row["_id"], "session_id": row.get("session_id"), "applied": {"$ne": True}},
                {"$set": {"session_id": session_id, "rebound_at": _now().isoformat()},
                 "$push": {"superseded_session_ids": row.get("session_id")}})
            if not res.modified_count:          # raced — re-dispatch on the fresh state
                return await fulfil_paid_session(session_id, metadata=md, **kw)
            sub = await apply_successful_payment(session_id, **kw)
            return {"outcome": "rebound", "subscription": sub}

    user_id, plan_id = md.get("user_id"), md.get("plan_id")
    plan = get_plan(plan_id or "")
    user = None
    if user_id and plan:
        from bson import ObjectId
        try:
            user = await db.users.find_one({"_id": ObjectId(user_id)}, {"_id": 1, "email": 1})
        except Exception:  # noqa: BLE001
            user = None
    if not user:
        return await _record_orphan_payment(
            db, session_id, reason=("metadata_not_rebuildable" if not (user_id and plan) else "user_not_found"),
            metadata=md, source=source, paid_amount_minor=paid_amount_minor, paid_currency=paid_currency)

    amount_minor = _int_or_none(md.get("amount_minor"))
    pv = _int_or_none(md.get("pricing_version"))
    months = _int_or_none(md.get("duration_months")) or plan.duration_months
    now = _now().isoformat()
    doc = {"user_id": user_id, "user_email": md.get("user_email") or user.get("email") or "",
           "plan_id": plan.id, "session_id": session_id,
           "amount_usd": (amount_minor / 100.0) if amount_minor is not None else plan.amount_usd,
           "amount_minor": amount_minor, "currency": str(md.get("currency") or paid_currency or "usd").lower(),
           "pricing_version": pv, "metadata": md, "payment_status": "initiated",
           "idempotency_key": key,
           "snapshot": {"plan_id": plan.id, "tier": plan.tier, "duration_months": months,
                        "pricing_version": pv, "idempotency_key": key},
           "rebuilt_from_metadata": True, "rebuilt_at": now, "created_at": now}
    try:
        await db.payment_transactions.update_one({"session_id": session_id}, {"$setOnInsert": doc}, upsert=True)
    except Exception:  # noqa: BLE001 — a concurrent rebuild won the unique(session_id) upsert
        pass
    logger.warning("rebuilt payment_transactions row from Stripe metadata session=%s user=%s plan=%s",
                   session_id, user_id, plan.id)
    sub = await apply_successful_payment(session_id, **kw)
    return {"outcome": "rebuilt", "subscription": sub}


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
        # audit r29 P2-03 — the user bought under the checkout snapshot: tier/duration/base
        # prices for proration come from it, never from the current catalog.
        snap = txn.get("snapshot") or {}
        bought_tier = snap.get("tier") or plan.tier
        bought_months = int(snap.get("duration_months") or plan.duration_months)
        base_cents = snap.get("tier_base_cents") or TIER_BASE_CENTS

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
                  "last_session_id": session_id,
                  "fulfilled_pricing_version": snap.get("pricing_version")}
        if active_remaining and cur_plan and cur_plan.tier != bought_tier:
            cur_rank = TIER_RANK[canonical_tier(cur_plan.tier)]
            new_rank = TIER_RANK[canonical_tier(bought_tier)]
            if new_rank > cur_rank:
                # UPGRADE — convert remaining time into equal-value new-tier days
                remaining_days = (current_vu - now).total_seconds() / 86400.0
                credit_days = remaining_days * (
                    base_cents[cur_plan.tier] / base_cents[bought_tier])
                new_vu = (now + relativedelta(months=bought_months)
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
                sched_vu = current_vu + relativedelta(months=bought_months)
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
            new_vu = base + relativedelta(months=bought_months)
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
