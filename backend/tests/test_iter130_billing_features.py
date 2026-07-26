"""iter-130 — Billing history UI backend, upgrade-preview endpoint,
renewal-reminder EMAIL in the billing sweep.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


# ---------------------------------------------------------------- reminder email

def test_renewal_sweep_sends_email_and_dedupes(monkeypatch):
    from background_loops import renewal_reminder_sweep
    import email_sender

    sent = []

    async def _fake_send(recipient, subject, html, text=None, sender=None):
        sent.append({"to": recipient, "subject": subject, "html": html})
        return {"ok": True, "id": "fake"}

    monkeypatch.setattr(email_sender, "send_email", _fake_send)
    monkeypatch.setattr(email_sender, "is_configured", lambda: True)

    async def _t():
        db = _db()
        from bson import ObjectId
        uid = ObjectId()
        email = f"reminder-{uid}@test.local"
        await db.users.insert_one({"_id": uid, "email": email,
                                   "name": "Rem Tester", "role": "user"})
        vu = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        await db.subscriptions.insert_one({
            "user_id": str(uid), "current_plan_id": "trader_monthly",
            "valid_until": vu,
        })
        try:
            n1 = await renewal_reminder_sweep(db)
            assert n1 >= 1
            mine = [s for s in sent if s["to"] == email]
            assert len(mine) == 1
            assert "renew" in mine[0]["subject"].lower()
            assert "Trader Monthly" in mine[0]["html"]
            notif = await db.notifications.find_one(
                {"user_id": str(uid), "type": "renewal_reminder"})
            assert notif is not None
            # dedupe: second sweep for the same valid_until sends nothing new
            sent.clear()
            await renewal_reminder_sweep(db)
            assert [s for s in sent if s["to"] == email] == []
        finally:
            await db.users.delete_one({"_id": uid})
            await db.subscriptions.delete_many({"user_id": str(uid)})
            await db.notifications.delete_many({"user_id": str(uid)})
    _run(_t())


def test_renewal_sweep_skips_admin_grandfather():
    from background_loops import renewal_reminder_sweep

    async def _t():
        db = _db()
        from bson import ObjectId
        uid = ObjectId()
        vu = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        await db.subscriptions.insert_one({
            "user_id": str(uid), "current_plan_id": "admin_grandfather",
            "valid_until": vu,
        })
        try:
            await renewal_reminder_sweep(db)
            notif = await db.notifications.find_one(
                {"user_id": str(uid), "type": "renewal_reminder"})
            assert notif is None
        finally:
            await db.subscriptions.delete_many({"user_id": str(uid)})
            await db.notifications.delete_many({"user_id": str(uid)})
    _run(_t())


# ---------------------------------------------------------------- upgrade preview

def _mk_user_with_sub(plan_id, days_left):
    from bson import ObjectId
    db = _db()

    async def _t():
        uid = ObjectId()
        await db.users.insert_one({"_id": uid, "email": f"up-{uid}@test.local",
                                   "role": "user", "email_verified": True})
        vu = (datetime.now(timezone.utc) + timedelta(days=days_left)).isoformat()
        await db.subscriptions.insert_one({
            "user_id": str(uid), "current_plan_id": plan_id, "valid_until": vu,
        })
        return str(uid)
    return _run(_t())


def _cleanup_user(uid):
    from bson import ObjectId
    db = _db()

    async def _t():
        await db.users.delete_one({"_id": ObjectId(uid)})
        await db.subscriptions.delete_many({"user_id": uid})
    _run(_t())


def test_upgrade_preview_logic():
    from routes.subscription_routes import upgrade_preview

    uid = _mk_user_with_sub("trader_monthly", days_left=15)
    try:
        out = _run(upgrade_preview(user={"id": uid}))
        assert out["active"] is True
        pv = out["previews"]
        # higher tier → upgrade with credited days (trader 9900 → pro 19900)
        up = pv["professional_monthly"]
        assert up["kind"] == "upgrade"
        expected = 15 * (9900 / 19900)
        assert abs(up["credited_days"] - expected) < 0.6
        # same tier → extend
        assert pv["trader_annual"]["kind"] == "extend"
        # lower tier → downgrade scheduled at current expiry
        dn = pv["starter_monthly"]
        assert dn["kind"] == "downgrade_scheduled"
        assert dn["starts_at"] == out["valid_until"]
    finally:
        _cleanup_user(uid)


def test_upgrade_preview_no_active_sub():
    from routes.subscription_routes import upgrade_preview

    uid = _mk_user_with_sub(None, days_left=-1)
    try:
        out = _run(upgrade_preview(user={"id": uid}))
        assert out["active"] is False
        assert all(p["kind"] == "new" for p in out["previews"].values())
    finally:
        _cleanup_user(uid)
