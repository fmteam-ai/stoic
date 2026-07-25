"""iter-122 — Billing correctness (Phase 1 of commercial hardening).

Covers:
  • Integer-cent price catalog (no float money at the source of truth).
  • Calendar-aware durations (annual = one calendar year, not 360 days).
  • Exactly-once payment application under webhook/poll race (atomic claim).
  • Stale-claim self-healing.
  • Upgrade proration (remaining value converts to new-tier days).
  • Downgrade scheduling (new pass starts when the current one ends).
  • Refund/chargeback revocation (duration pulled back, idempotent).
  • Affiliate commission idempotency + outbox retry + reversal.
  • Checkout origin allowlist (HTTP).
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))

from subscription_plans import PLANS, TIER_BASE_CENTS

API = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/") + "/api"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def svc_db():
    """Fresh motor client + reset of the database singleton so the
    subscription_service module binds to THIS test's event loop."""
    import database
    database._client = None
    database._db = None
    yield
    database._client = None
    database._db = None


def _now():
    return datetime.now(timezone.utc)


async def _mk_user(db, *, referred_by=None):
    from bson import ObjectId
    uid = ObjectId()
    doc = {"_id": uid, "email": f"iter122-{uuid.uuid4().hex[:8]}@example.com",
           "role": "user", "created_at": _now().isoformat()}
    if referred_by:
        doc["referred_by_code"] = referred_by
        doc["referred_at"] = _now().isoformat()
    await db.users.insert_one(doc)
    return str(uid)


async def _mk_txn(db, user_id, plan_id, session_id=None):
    sid = session_id or f"cs_test_{uuid.uuid4().hex}"
    plan = PLANS[plan_id]
    await db.payment_transactions.insert_one({
        "user_id": user_id, "user_email": "x@example.com",
        "plan_id": plan_id, "session_id": sid,
        "amount_usd": plan.amount_usd, "currency": "usd",
        "metadata": {}, "payment_status": "initiated",
        "created_at": _now().isoformat(),
    })
    return sid


# ============ Integer cents ============
def test_catalog_prices_are_integer_cents():
    assert TIER_BASE_CENTS == {"starter": 3900, "trader": 9900,
                               "professional": 19900, "elite_ai": 39900}
    for p in PLANS.values():
        assert isinstance(p.amount_cents, int)
        assert p.amount_usd == p.amount_cents / 100.0
        assert p.to_public()["amount_cents"] == p.amount_cents
    # exact discount math in cents — annual trader: 9900*12*0.60
    assert PLANS["trader_annual"].amount_cents == 71280
    assert PLANS["elite_ai_monthly"].amount_cents == 39900


# ============ Calendar-aware duration ============
def test_annual_pass_lasts_a_calendar_year(svc_db):
    async def inner():
        from database import get_db
        from subscription_service import apply_successful_payment
        db = get_db()
        uid = await _mk_user(db)
        sid = await _mk_txn(db, uid, "trader_annual")
        sub = await apply_successful_payment(sid, source="test")
        assert sub is not None
        vu = datetime.fromisoformat(sub["valid_until"])
        days = (vu - _now()).total_seconds() / 86400
        assert 364.5 <= days <= 366.5, f"annual pass = {days:.1f} days"
    _run(inner())


# ============ Exactly-once under race ============
def test_apply_is_exactly_once_under_race(svc_db):
    async def inner():
        from database import get_db
        from subscription_service import apply_successful_payment
        db = get_db()
        uid = await _mk_user(db)
        sid = await _mk_txn(db, uid, "trader_monthly")
        r1, r2 = await asyncio.gather(
            apply_successful_payment(sid, source="webhook"),
            apply_successful_payment(sid, source="poll"))
        applied = [r for r in (r1, r2) if r is not None]
        assert len(applied) == 1, "both webhook and poll applied the payment!"
        # duration extended exactly ONE month, not two
        vu = datetime.fromisoformat(applied[0]["valid_until"])
        days = (vu - _now()).total_seconds() / 86400
        assert 27 <= days <= 32, f"double-applied? pass = {days:.1f} days"
        # third call after completion → idempotent None
        assert await apply_successful_payment(sid, source="retry") is None
    _run(inner())


def test_stale_claim_self_heals(svc_db):
    async def inner():
        from database import get_db
        from subscription_service import apply_successful_payment
        db = get_db()
        uid = await _mk_user(db)
        sid = await _mk_txn(db, uid, "starter_monthly")
        # Simulate a crashed holder: claim taken 10 minutes ago, never applied
        stale = (_now() - timedelta(minutes=10)).isoformat()
        await db.payment_transactions.update_one(
            {"session_id": sid}, {"$set": {"apply_claimed_at": stale}})
        sub = await apply_successful_payment(sid, source="retry")
        assert sub is not None and sub.get("current_plan_id") == "starter_monthly"
    _run(inner())


# ============ Upgrade / downgrade proration ============
def test_upgrade_converts_remaining_value_to_new_tier_days(svc_db):
    async def inner():
        from database import get_db
        from subscription_service import apply_successful_payment
        db = get_db()
        uid = await _mk_user(db)
        # Active trader pass with ~30 days remaining
        vu = _now() + timedelta(days=30)
        await db.subscriptions.insert_one({
            "user_id": uid, "current_plan_id": "trader_monthly",
            "valid_until": vu.isoformat(), "auto_renew": False,
            "grace_until": None, "created_at": _now().isoformat()})
        sid = await _mk_txn(db, uid, "professional_monthly")
        sub = await apply_successful_payment(sid, source="test")
        assert sub["current_plan_id"] == "professional_monthly"
        new_vu = datetime.fromisoformat(sub["valid_until"])
        days = (new_vu - _now()).total_seconds() / 86400
        # 1 calendar month (~31d for most months) + 30 * 9900/19900 ≈ +14.9d
        expected_credit = 30 * 9900 / 19900
        assert 26 + expected_credit <= days <= 33 + expected_credit, days
        assert sub.get("proration", {}).get("kind") == "upgrade"
    _run(inner())


def test_downgrade_is_scheduled_after_current_pass(svc_db):
    async def inner():
        from database import get_db
        from subscription_service import apply_successful_payment, is_active
        db = get_db()
        uid = await _mk_user(db)
        vu = _now() + timedelta(days=10)
        await db.subscriptions.insert_one({
            "user_id": uid, "current_plan_id": "professional_monthly",
            "valid_until": vu.isoformat(), "auto_renew": False,
            "grace_until": None, "created_at": _now().isoformat()})
        sid = await _mk_txn(db, uid, "trader_monthly")
        sub = await apply_successful_payment(sid, source="test")
        # current pass untouched, new pass scheduled
        assert sub["current_plan_id"] == "professional_monthly"
        assert datetime.fromisoformat(sub["valid_until"]) == vu
        assert sub["scheduled_plan_id"] == "trader_monthly"
        sched = datetime.fromisoformat(sub["scheduled_valid_until"])
        assert sched > vu
        # Lazy promotion once the current pass lapses
        await db.subscriptions.update_one(
            {"user_id": uid},
            {"$set": {"valid_until": (_now() - timedelta(hours=1)).isoformat()}})
        state = await is_active(uid)
        assert state["active"] is True
        assert state["plan_id"] == "trader_monthly"
    _run(inner())


# ============ Refund / chargeback ============
def test_revoke_payment_pulls_duration_back(svc_db):
    async def inner():
        from database import get_db
        from subscription_service import apply_successful_payment, revoke_payment
        db = get_db()
        uid = await _mk_user(db)
        sid = await _mk_txn(db, uid, "trader_monthly")
        sub = await apply_successful_payment(sid, source="test")
        vu_before = datetime.fromisoformat(sub["valid_until"])
        out = await revoke_payment(sid, reason="charge.refunded")
        vu_after = datetime.fromisoformat(out["valid_until"])
        assert vu_after < vu_before
        assert vu_after <= _now() + timedelta(days=1)  # access gone
        txn = await db.payment_transactions.find_one({"session_id": sid})
        assert txn["revoked"] is True and txn["payment_status"] == "refunded"
        # idempotent — second revoke is a no-op
        assert await revoke_payment(sid, reason="again") is None
    _run(inner())


# ============ Affiliate idempotency / outbox / reversal ============
async def _mk_affiliate(db):
    from bson import ObjectId
    owner = await _mk_user(db)
    code = f"T{uuid.uuid4().hex[:7].upper()}"
    res = await db.affiliates.insert_one({
        "user_id": owner, "code": code, "active": True,
        "lifetime_earnings_usd": 0.0, "unpaid_balance_usd": 0.0,
        "created_at": _now().isoformat()})
    return code, res.inserted_id


def test_commission_recorded_exactly_once(svc_db):
    async def inner():
        from database import get_db
        from affiliate_service import record_commission_if_referred
        db = get_db()
        code, af_id = await _mk_affiliate(db)
        buyer = await _mk_user(db, referred_by=code)
        sid = f"cs_test_{uuid.uuid4().hex}"
        r1 = await record_commission_if_referred(
            user_id=buyer, plan_id="trader_monthly", amount_usd=99.0,
            session_id=sid)
        r2 = await record_commission_if_referred(
            user_id=buyer, plan_id="trader_monthly", amount_usd=99.0,
            session_id=sid)
        assert r1 is not None and r2 is None
        n = await db.affiliate_commissions.count_documents(
            {"session_id": sid, "tier": 1})
        assert n == 1
        af = await db.affiliates.find_one({"_id": af_id})
        assert abs(af["unpaid_balance_usd"] - 19.80) < 0.001  # 20% of $99
    _run(inner())


def test_outbox_processing_and_reversal(svc_db):
    async def inner():
        from database import get_db
        from affiliate_service import (process_affiliate_outbox,
                                       reverse_commissions_for_session)
        db = get_db()
        code, af_id = await _mk_affiliate(db)
        buyer = await _mk_user(db, referred_by=code)
        sid = f"cs_test_{uuid.uuid4().hex}"
        await db.affiliate_outbox.insert_one({
            "session_id": sid, "user_id": buyer, "plan_id": "trader_monthly",
            "amount_usd": 99.0, "status": "pending", "attempts": 0,
            "created_at": _now().isoformat()})
        out = await process_affiliate_outbox(db, only_session=sid)
        assert out["processed"] == 1
        entry = await db.affiliate_outbox.find_one({"session_id": sid})
        assert entry["status"] == "done"
        # Reversal pulls the money back and marks the row
        n = await reverse_commissions_for_session(db, sid, reason="refund")
        assert n == 1
        c = await db.affiliate_commissions.find_one({"session_id": sid, "tier": 1})
        assert c["status"] == "reversed"
        af = await db.affiliates.find_one({"_id": af_id})
        assert abs(af["unpaid_balance_usd"]) < 0.001
        # idempotent
        assert await reverse_commissions_for_session(db, sid) == 0
    _run(inner())


def test_payment_ledger_unique_index(svc_db):
    async def inner():
        from database import get_db
        db = get_db()
        sid = f"cs_test_{uuid.uuid4().hex}"
        await db.payment_transactions.insert_one(
            {"session_id": sid, "user_id": "x", "created_at": _now().isoformat()})
        with pytest.raises(Exception):
            await db.payment_transactions.insert_one(
                {"session_id": sid, "user_id": "y",
                 "created_at": _now().isoformat()})
    _run(inner())


# ============ Origin allowlist (HTTP) ============
def test_checkout_rejects_unlisted_origin():
    from tests.helpers import register_and_login
    s = register_and_login(f"iter122-origin-{uuid.uuid4().hex[:8]}@example.com")
    r = s.post(f"{API}/subscription/checkout",
               json={"plan_id": "starter_monthly",
                     "origin": "https://evil.example.com"}, timeout=15)
    assert r.status_code == 400
    assert "origin" in r.text.lower()
    # The configured frontend origin IS accepted (reaches Stripe)
    good = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
    r2 = s.post(f"{API}/subscription/checkout",
                json={"plan_id": "starter_monthly", "origin": good}, timeout=30)
    assert r2.status_code == 200, r2.text
    assert r2.json()["plan"]["amount_usd"] == 39.0
