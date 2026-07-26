"""iter-135 — Help Center portal backend: support tickets, public status,
legal documents, onboarding wizard state."""
import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

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


def _mk_user(role="user"):
    from bson import ObjectId
    db = _db()

    async def _t():
        uid = ObjectId()
        await db.users.insert_one({"_id": uid, "email": f"pt-{uid}@test.local",
                                   "role": role, "email_verified": True})
        return {"id": str(uid), "email": f"pt-{uid}@test.local", "role": role}
    return _run(_t())


def _cleanup(uids):
    from bson import ObjectId
    db = _db()

    async def _t():
        for u in uids:
            await db.users.delete_one({"_id": ObjectId(u["id"])})
            await db.support_tickets.delete_many({"user_id": u["id"]})
    _run(_t())


def test_legal_documents():
    from legal_content import get_legal
    for kind, marker in (("privacy", "Privacy Policy"),
                         ("risk", "Risk Disclosure")):
        doc = get_legal(kind)
        assert doc and marker in doc["markdown"]
        assert doc["version"]
    assert get_legal("nope") is None


def test_public_status_shape():
    from routes.portal_routes import public_status, _STATUS_CACHE
    _STATUS_CACHE.update(at=0.0, data=None)
    out = _run(public_status())
    assert out["overall"] in ("operational", "degraded", "major_outage")
    for key in ("api", "database", "bot_engine", "ea_bridge",
                "payments", "email"):
        assert key in out["components"]
    assert out["components"]["database"]["status"] == "operational"
    # cached second call returns same object
    out2 = _run(public_status())
    assert out2 is out


def test_ticket_lifecycle(monkeypatch):
    import email_sender
    sent = []

    async def _fake_send(recipient, subject, html, text=None, sender=None):
        sent.append(recipient)
        return {"ok": True}
    monkeypatch.setattr(email_sender, "send_email", _fake_send)
    monkeypatch.setattr(email_sender, "is_configured", lambda: True)

    from routes.support_routes import (
        create_ticket, my_tickets, get_ticket, reply_ticket,
        close_ticket, admin_tickets,
    )
    user = _mk_user()
    other = _mk_user()
    admin = _mk_user(role="admin")
    try:
        t = _run(create_ticket(
            {"category": "billing", "subject": "Refund please",
             "message": "I want a refund for my annual plan."},
            request=None, user=user))
        assert t["status"] == "open" and t["message_count"] == 1
        tid = t["id"]

        # owner sees it; stranger gets 404
        mine = _run(my_tickets(user=user))
        assert any(x["id"] == tid for x in mine)
        with pytest.raises(HTTPException) as e:
            _run(get_ticket(tid, user=other))
        assert e.value.status_code == 404

        # admin reply -> answered + email to user
        t2 = _run(reply_ticket(tid, {"message": "Refund processed."},
                               request=None, user=admin))
        assert t2["status"] == "answered"
        assert user["email"] in sent

        # user reply -> back to open
        t3 = _run(reply_ticket(tid, {"message": "Thanks!"},
                               request=None, user=user))
        assert t3["status"] == "open" and t3["message_count"] == 3

        # admin queue
        q = _run(admin_tickets(status="open", user=admin))
        assert any(x["id"] == tid for x in q["tickets"])
        with pytest.raises(HTTPException):
            _run(admin_tickets(status="open", user=user))

        # close; replies rejected after
        _run(close_ticket(tid, user=user))
        with pytest.raises(HTTPException) as e:
            _run(reply_ticket(tid, {"message": "more"}, request=None, user=user))
        assert e.value.status_code == 400
    finally:
        _cleanup([user, other, admin])


def test_ticket_validation():
    from routes.support_routes import create_ticket
    user = _mk_user()
    try:
        with pytest.raises(HTTPException):
            _run(create_ticket({"category": "bogus", "subject": "abc",
                                "message": "long enough message"},
                               request=None, user=user))
        with pytest.raises(HTTPException):
            _run(create_ticket({"category": "billing", "subject": "ab",
                                "message": "short"},
                               request=None, user=user))
    finally:
        _cleanup([user])


def test_onboarding_state_and_risk_apply():
    from routes.portal_routes import onboarding_state, onboarding_update
    from bson import ObjectId
    db = _db()
    user = _mk_user()
    try:
        # fresh user with no accounts -> pending
        ob = _run(onboarding_state(user=user))
        assert ob["status"] == "pending" and ob["step"] == 0

        # default bot config exists per registration flow — create stub
        async def _cfg():
            await db.bot_configs.insert_one(
                {"user_id": user["id"], "account_id": None,
                 "risk_level": "medium", "active": False})
        _run(_cfg())

        ob2 = _run(onboarding_update({"step": 2, "risk_level": "high"},
                                     user=user))
        assert ob2["step"] == 2 and ob2["risk_level"] == "high"

        async def _check():
            cfg = await db.bot_configs.find_one(
                {"user_id": user["id"], "account_id": None})
            return cfg["risk_level"]
        assert _run(_check()) == "high"

        ob3 = _run(onboarding_update({"status": "done"}, user=user))
        assert ob3["status"] == "done"

        with pytest.raises(HTTPException):
            _run(onboarding_update({"status": "weird"}, user=user))
    finally:
        async def _clean():
            await db.bot_configs.delete_many({"user_id": user["id"]})
        _run(_clean())
        _cleanup([user])


def test_onboarding_existing_trader_auto_done():
    from routes.portal_routes import onboarding_state
    db = _db()
    user = _mk_user()
    try:
        async def _acct():
            await db.accounts.insert_one({"user_id": user["id"],
                                          "display_name": "t"})
        _run(_acct())
        ob = _run(onboarding_state(user=user))
        assert ob["status"] == "done"
    finally:
        async def _clean():
            await db.accounts.delete_many({"user_id": user["id"]})
        _run(_clean())
        _cleanup([user])
