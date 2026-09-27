"""Alert Test Button (P1 backlog) — email test-alert endpoint, per-channel
last_test stamping, rate limit, fail-closed when email is unconfigured."""
import os
import sys

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.critical_controls]
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))


def _db():
    from database import get_db
    return get_db()


def _run(coro):
    from conftest import run_async
    return run_async(coro)


class _Req:
    headers = {}


@pytest.fixture
def user(request):
    from database import get_db
    from bson import ObjectId
    uid = str(ObjectId())
    u = {"id": uid, "email": f"alert-{uid}@test.local", "name": "Alert Tester", "role": "user"}
    from bson import ObjectId as _O
    _run(get_db().users.insert_one({"_id": _O(uid), "email": u["email"], "role": "user", "email_verified": True}))

    def _cleanup():
        db = get_db()
        _run(db.notifications.delete_many({"user_id": uid}))
        _run(db.users.delete_one({"_id": _O(uid)}))
        _run(db.notification_test_audit.delete_many({"actor_email": u["email"]}))
        _run(db.rate_limits.delete_many({"_id": {"$regex": f"^alert_test_.*:{uid}:"}}))
    request.addfinalizer(_cleanup)
    return u


def test_email_test_sends_and_stamps_last_test(monkeypatch, user):
    from routes import notification_routes as nr
    import email_sender
    sent = []

    async def _fake_send(recipient, subject, html, text=None, sender=None):
        sent.append({"to": recipient, "subject": subject, "html": html})
        return {"ok": True, "id": "fake"}

    monkeypatch.setattr(email_sender, "send_email", _fake_send)
    monkeypatch.setattr(email_sender, "is_configured", lambda: True)

    res = _run(nr.test_email(_Req(), user=user))
    assert res["ok"] is True and res["channel"] == "email"
    assert res["delivered_to"].startswith("al***@")
    assert sent and sent[0]["to"] == user["email"]
    assert sent[0]["subject"].startswith("[TEST]")
    audit = _run(_db().notification_test_audit.find_one({"actor_email": user["email"]}))
    assert audit and audit["meta"]["ok"] is True and audit["entry_hash"]
    assert "Alert Tester" in sent[0]["html"]

    cfg = _run(nr.get_telegram(user=user))
    assert cfg["email_configured"] is True
    assert cfg["last_test"]["email"]["ok"] is True
    assert cfg["last_test"]["email"]["at"]


def test_email_test_fails_closed_when_unconfigured(monkeypatch, user):
    from routes import notification_routes as nr
    import email_sender
    monkeypatch.setattr(email_sender, "is_configured", lambda: False)

    with pytest.raises(HTTPException) as ei:
        _run(nr.test_email(_Req(), user=user))
    assert ei.value.status_code == 503
    assert ei.value.detail["code"] == "email_not_configured"
    cfg = _run(nr.get_telegram(user=user))
    lt = cfg["last_test"]["email"]
    assert lt["ok"] is False and lt["error"] == "email_not_configured" and lt["at"]


def test_email_test_provider_failure_is_502(monkeypatch, user):
    from routes import notification_routes as nr
    import email_sender

    async def _fail(*a, **k):
        return {"ok": False, "error": "domain not verified"}

    monkeypatch.setattr(email_sender, "send_email", _fail)
    monkeypatch.setattr(email_sender, "is_configured", lambda: True)
    with pytest.raises(HTTPException) as ei:
        _run(nr.test_email(_Req(), user=user))
    assert ei.value.status_code == 503
    assert "domain" in ei.value.detail["reason"]
    cfg = _run(nr.get_telegram(user=user))
    assert cfg["last_test"]["email"]["ok"] is False
    assert cfg["last_test"]["email"]["error"] == "email_send_failed"


def test_email_test_rate_limited(monkeypatch, user):
    from routes import notification_routes as nr
    import email_sender

    async def _ok(*a, **k):
        return {"ok": True, "id": "x"}

    monkeypatch.setattr(email_sender, "send_email", _ok)
    monkeypatch.setattr(email_sender, "is_configured", lambda: True)
    for _ in range(nr.TEST_ALERT_MAX):
        _run(nr.test_email(_Req(), user=user))
    with pytest.raises(HTTPException) as ei:
        _run(nr.test_email(_Req(), user=user))
    assert ei.value.status_code == 429


def test_telegram_test_requires_verified_chat_then_unconfigured(user):
    from routes import notification_routes as nr
    # r15 P2-02 — an unverified chat is refused BEFORE any provider call
    with pytest.raises(HTTPException) as ei:
        _run(nr.test_telegram(_Req(), user=user))
    assert ei.value.status_code == 403 and ei.value.detail["code"] == "destination_unverified"
    _run(_db().notifications.update_one({"user_id": user["id"]}, {"$set": {"telegram_verified": True}}, upsert=True))
    with pytest.raises(HTTPException) as ei2:
        _run(nr.test_telegram(_Req(), user=user))
    assert ei2.value.status_code == 400
    cfg = _run(nr.get_telegram(user=user))
    assert cfg["last_test"]["telegram"]["error"] == "telegram_not_configured"


def test_email_test_refuses_legacy_unverified_state(monkeypatch, user):
    from routes import notification_routes as nr
    import email_sender
    monkeypatch.setattr(email_sender, "is_configured", lambda: True)
    _run(_db().users.update_one({"email": user["email"]}, {"$unset": {"email_verified": ""}}))
    with pytest.raises(HTTPException) as ei:
        _run(nr.test_email(_Req(), user=user))
    assert ei.value.status_code == 403 and ei.value.detail["code"] == "destination_unverified"


def test_telegram_verify_handshake(monkeypatch, user):
    from routes import notification_routes as nr
    sent = {}

    async def _fake_send(db, uid, text):
        sent["text"] = text
        return True, "42", None
    monkeypatch.setattr(nr, "_telegram_send", _fake_send)
    _run(_db().notifications.update_one({"user_id": user["id"]}, {"$set": {"telegram_bot_token": "x", "telegram_chat_id": "1"}}, upsert=True))
    r = _run(nr.telegram_verify_start(_Req(), user=user))
    assert r["sent"] is True and r["provider_receipt"] == "42"
    import re as _re
    code = _re.search(r"\*(\d{6})\*", sent["text"]).group(1)
    with pytest.raises(HTTPException) as ei:
        _run(nr.telegram_verify_confirm({"code": "000000" if code != "000000" else "111111"}, _Req(), user=user))
    assert ei.value.detail["code"] == "wrong_code"
    assert _run(nr.telegram_verify_confirm({"code": code}, _Req(), user=user))["telegram_verified"] is True
    assert _run(nr.get_telegram(user=user))["telegram_verified"] is True
    # rebinding the chat resets verification
    _run(nr.update_telegram(nr.TelegramConfigIn(telegram_chat_id="2"), user=user))
    assert _run(nr.get_telegram(user=user))["telegram_verified"] is False


def test_mask_email():
    from routes.notification_routes import _mask_email
    assert _mask_email("trader@stoicaibot.com") == "tr***@stoicaibot.com"
    assert _mask_email("") == "***"
