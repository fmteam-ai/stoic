"""Affiliate Program — application, attribution, and commission tracking.

Data model (4 collections):
  - affiliate_applications  : pending applications awaiting admin review
  - affiliates              : approved affiliates with referral codes
  - affiliate_clicks        : every /r/{code} hit, IP-hashed for de-dupe
  - affiliate_commissions   : commission rows generated on successful payments

Commission model:
  20% recurring of the base subscription fee on every paid renewal of a
  referred user. Cookie attribution lasts 60 days from first click. First
  paid checkout within attribution window locks in the affiliate.
"""
import hashlib
import secrets
import string
from datetime import datetime, timezone, timedelta
from typing import Optional
from bson import ObjectId
from pymongo import ReturnDocument
from database import get_db
from subscription_plans import get_plan


COMMISSION_RATE = 0.20         # 20% recurring (tier 1)
TIER2_OVERRIDE_RATE = 0.05     # 5% override on a sub-affiliate's commissions (tier 2)
COOKIE_TTL_DAYS = 60           # 60-day attribution window
MIN_PAYOUT_USD = 50.0          # minimum payout threshold


def _now():
    return datetime.now(timezone.utc)


def _code() -> str:
    """6-char uppercase alphanumeric referral code."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(6))


def _hash_ip(ip: str) -> str:
    return hashlib.sha256(f"stoic-aff:{ip}".encode()).hexdigest()[:32]


# --- Application lifecycle -------------------------------------------------
async def submit_application(*, user_id: str, payload: dict) -> dict:
    db = get_db()
    existing = await db.affiliate_applications.find_one(
        {"user_id": user_id, "status": {"$in": ["pending", "approved"]}}
    )
    if existing:
        return {"duplicate": True, "status": existing["status"],
                "id": str(existing["_id"])}

    doc = {
        "user_id": user_id,
        "user_email": payload.get("user_email"),
        "full_name": payload.get("full_name"),
        "audience_url": payload.get("audience_url"),
        "audience_size": payload.get("audience_size"),
        "promotion_strategy": payload.get("promotion_strategy"),
        "payment_method": payload.get("payment_method"),
        "payment_details": payload.get("payment_details"),
        "terms_agreed_at": _now().isoformat(),
        "terms_version": payload.get("terms_version", "2026-06-22"),
        "status": "pending",
        "submitted_at": _now().isoformat(),
    }
    r = await db.affiliate_applications.insert_one(doc)
    return {"duplicate": False, "id": str(r.inserted_id), "status": "pending"}


async def get_application(user_id: str) -> Optional[dict]:
    db = get_db()
    doc = await db.affiliate_applications.find_one(
        {"user_id": user_id}, sort=[("submitted_at", -1)]
    )
    if not doc:
        return None
    doc["id"] = str(doc.pop("_id"))
    return doc


async def get_affiliate(user_id: str) -> Optional[dict]:
    db = get_db()
    doc = await db.affiliates.find_one({"user_id": user_id})
    if not doc:
        return None
    doc["id"] = str(doc.pop("_id"))
    return doc


async def approve_application(application_id: str, admin_email: str) -> dict:
    db = get_db()
    try:
        oid = ObjectId(application_id)
    except Exception:
        return {"ok": False, "error": "invalid id"}
    app_doc = await db.affiliate_applications.find_one_and_update(
        {"_id": oid, "status": "pending"},
        {"$set": {"status": "approved",
                  "approved_at": _now().isoformat(),
                  "approved_by": admin_email}},
        return_document=ReturnDocument.AFTER,
    )
    if not app_doc:
        return {"ok": False, "error": "application not found or not pending"}

    # Generate a unique code
    for _ in range(10):
        code = _code()
        existing = await db.affiliates.find_one({"code": code})
        if not existing:
            break
    else:
        return {"ok": False, "error": "could not allocate referral code"}

    affiliate = {
        "user_id": app_doc["user_id"],
        "user_email": app_doc.get("user_email"),
        "full_name": app_doc.get("full_name"),
        "code": code,
        "application_id": str(oid),
        "active": True,
        "payment_method": app_doc.get("payment_method"),
        "payment_details": app_doc.get("payment_details"),
        "created_at": _now().isoformat(),
        "lifetime_clicks": 0,
        "lifetime_conversions": 0,
        "lifetime_earnings_usd": 0.0,
        "unpaid_balance_usd": 0.0,
    }

    # 2-tier link: if this newly-approved affiliate was themselves referred by
    # another active affiliate, record the parent so future commissions on
    # this affiliate's sales pay a 5% override to the parent.
    user_doc = await db.users.find_one({"_id": ObjectId(app_doc["user_id"])})
    if user_doc and user_doc.get("referred_by_code"):
        parent = await db.affiliates.find_one(
            {"code": user_doc["referred_by_code"].upper(), "active": True}
        )
        if parent and parent["user_id"] != app_doc["user_id"]:
            affiliate["parent_affiliate_id"] = str(parent["_id"])
            affiliate["parent_affiliate_code"] = parent["code"]

    await db.affiliates.insert_one(affiliate)
    return {"ok": True, "code": code, "affiliate": {**affiliate, "id": "newly_created"}}


async def reject_application(application_id: str, admin_email: str, reason: str = "") -> dict:
    db = get_db()
    try:
        oid = ObjectId(application_id)
    except Exception:
        return {"ok": False, "error": "invalid id"}
    res = await db.affiliate_applications.update_one(
        {"_id": oid, "status": "pending"},
        {"$set": {"status": "rejected",
                  "rejected_at": _now().isoformat(),
                  "rejected_by": admin_email,
                  "rejection_reason": reason}},
    )
    return {"ok": bool(res.modified_count)}


async def list_applications(status: Optional[str] = None) -> list[dict]:
    db = get_db()
    q = {"status": status} if status else {}
    cursor = db.affiliate_applications.find(q).sort("submitted_at", -1).limit(200)
    docs = await cursor.to_list(length=200)
    for d in docs:
        d["id"] = str(d.pop("_id"))
    return docs


# --- Attribution ----------------------------------------------------------
async def record_click(*, code: str, ip: str, user_agent: str, referrer: str = "") -> Optional[dict]:
    """Capture a referral click — returns the affiliate doc if code valid."""
    db = get_db()
    affiliate = await db.affiliates.find_one({"code": code.upper(), "active": True})
    if not affiliate:
        return None
    ip_hash = _hash_ip(ip)
    await db.affiliate_clicks.insert_one({
        "affiliate_id": str(affiliate["_id"]),
        "code": code.upper(),
        "ip_hash": ip_hash,
        "user_agent": (user_agent or "")[:300],
        "referrer": (referrer or "")[:300],
        "at": _now().isoformat(),
    })
    await db.affiliates.update_one(
        {"_id": affiliate["_id"]},
        {"$inc": {"lifetime_clicks": 1}},
    )
    affiliate["id"] = str(affiliate.pop("_id"))
    return affiliate


# --- Commission recording (called from apply_successful_payment) ----------
async def record_commission_if_referred(*, user_id: str, plan_id: str,
                                        amount_usd: float, session_id: str) -> Optional[dict]:
    db = get_db()
    # Look up the buyer's attribution
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user or not user.get("referred_by_code"):
        return None
    code = user["referred_by_code"]
    # Check attribution window: 60d from referral click
    ref_at_raw = user.get("referred_at")
    if not ref_at_raw:
        return None
    try:
        ref_at = datetime.fromisoformat(ref_at_raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if _now() - ref_at > timedelta(days=COOKIE_TTL_DAYS):
        return None  # attribution expired
    # Self-referral guard
    affiliate = await db.affiliates.find_one({"code": code.upper(), "active": True})
    if not affiliate:
        return None
    if affiliate["user_id"] == user_id:
        return None  # never reward self-referrals
    plan = get_plan(plan_id)
    if not plan:
        return None
    commission_usd = round(amount_usd * COMMISSION_RATE, 2)
    is_first = not user.get("first_paid_at")
    doc = {
        "affiliate_id": str(affiliate["_id"]),
        "affiliate_code": code,
        "referred_user_id": user_id,
        "referred_user_email": user.get("email"),
        "plan_id": plan_id,
        "session_id": session_id,
        "sale_amount_usd": amount_usd,
        "commission_usd": commission_usd,
        "rate": COMMISSION_RATE,
        "tier": 1,
        "is_first_payment": is_first,
        "status": "pending",  # 'pending' until payout; 'paid' after admin payout
        "created_at": _now().isoformat(),
    }
    res = await db.affiliate_commissions.insert_one(doc)
    tier1_commission_id = str(res.inserted_id)
    await db.affiliates.update_one(
        {"_id": affiliate["_id"]},
        {"$inc": {
            "lifetime_earnings_usd": commission_usd,
            "unpaid_balance_usd": commission_usd,
            **({"lifetime_conversions": 1} if is_first else {}),
        }},
    )

    # Tier-2 override: if THIS affiliate was themselves referred by a parent
    # affiliate, the parent earns 5% on the same sale as a hands-off override.
    if affiliate.get("parent_affiliate_id"):
        parent = await db.affiliates.find_one(
            {"_id": ObjectId(affiliate["parent_affiliate_id"]), "active": True}
        )
        if parent:
            override_usd = round(amount_usd * TIER2_OVERRIDE_RATE, 2)
            await db.affiliate_commissions.insert_one({
                "affiliate_id": str(parent["_id"]),
                "affiliate_code": parent["code"],
                "referred_user_id": user_id,
                "referred_user_email": user.get("email"),
                "plan_id": plan_id,
                "session_id": session_id,
                "sale_amount_usd": amount_usd,
                "commission_usd": override_usd,
                "rate": TIER2_OVERRIDE_RATE,
                "tier": 2,
                "tier1_commission_id": tier1_commission_id,
                "tier1_affiliate_id": str(affiliate["_id"]),
                "tier1_affiliate_code": code,
                "is_first_payment": is_first,
                "status": "pending",
                "created_at": _now().isoformat(),
            })
            await db.affiliates.update_one(
                {"_id": parent["_id"]},
                {"$inc": {
                    "lifetime_earnings_usd": override_usd,
                    "unpaid_balance_usd": override_usd,
                }},
            )

    if is_first:
        await db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"first_paid_at": _now().isoformat()}},
        )
    return doc


# --- Stats for affiliate dashboard ----------------------------------------
async def stats_for(user_id: str) -> dict:
    db = get_db()
    affiliate = await db.affiliates.find_one({"user_id": user_id})
    if not affiliate:
        return {"affiliate": None}
    afid = str(affiliate["_id"])
    pending = await db.affiliate_commissions.count_documents(
        {"affiliate_id": afid, "status": "pending"}
    )
    paid = await db.affiliate_commissions.count_documents(
        {"affiliate_id": afid, "status": "paid"}
    )
    recent = await db.affiliate_commissions.find(
        {"affiliate_id": afid}, sort=[("created_at", -1)]
    ).limit(20).to_list(length=20)
    for r in recent:
        r["id"] = str(r.pop("_id"))
    affiliate["id"] = str(affiliate.pop("_id"))

    # Tier-2 metrics: how many affiliates was this affiliate the parent of,
    # and how much did they generate in override commissions.
    sub_affiliates_cursor = db.affiliates.find(
        {"parent_affiliate_id": afid, "active": True}
    )
    subs = await sub_affiliates_cursor.to_list(length=200)
    sub_summary = []
    tier2_earnings = 0.0
    for s in subs:
        sid = str(s["_id"])
        # Sum tier-2 override commissions credited to this affiliate FROM this sub
        cursor = db.affiliate_commissions.find(
            {"affiliate_id": afid, "tier": 2, "tier1_affiliate_id": sid}
        )
        rows = await cursor.to_list(length=500)
        sub_total = round(sum(r.get("commission_usd", 0) for r in rows), 2)
        tier2_earnings += sub_total
        sub_summary.append({
            "code": s.get("code"),
            "user_email": s.get("user_email"),
            "active": s.get("active"),
            "lifetime_conversions": s.get("lifetime_conversions", 0),
            "override_earnings_usd": sub_total,
            "override_count": len(rows),
        })

    return {
        "affiliate": affiliate,
        "pending_commissions": pending,
        "paid_commissions": paid,
        "min_payout_usd": MIN_PAYOUT_USD,
        "commission_rate": COMMISSION_RATE,
        "tier2_override_rate": TIER2_OVERRIDE_RATE,
        "cookie_ttl_days": COOKIE_TTL_DAYS,
        "recent_commissions": recent,
        "sub_affiliates": sub_summary,
        "tier2_earnings_usd": round(tier2_earnings, 2),
    }
