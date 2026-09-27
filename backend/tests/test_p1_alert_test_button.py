"""Alert Test Button (P1 backlog) — email test-alert endpoint, per-channel
last_test stamping, rate limit, fail-closed when email is unconfigured."""
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))


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

    def _cleanup():
        db = get_db()
        _run(db.notifications.delete_many({"user_id": uid}))
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
    assert "test alert" in sent[0]["subject"].lower()
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
    assert cfg["last_test"]["email"] == {"ok": False, "error": "email_not_configured",
                                         "at": cfg["last_test"]["email"]["at"]}


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
    assert cfg["last_test"]["email"]["error"] == "send_failed"


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


def test_telegram_test_unconfigured_stamps_failure(user):
    from routes import notification_routes as nr
    with pytest.raises(HTTPException) as ei:
        _run(nr.test_telegram(_Req(), user=user))
    assert ei.value.status_code == 400
    cfg = _run(nr.get_telegram(user=user))
    assert cfg["last_test"]["telegram"]["error"] == "telegram_not_configured"


def test_mask_email():
    from routes.notification_routes import _mask_email
    assert _mask_email("trader@stoicaibot.com") == "tr***@stoicaibot.com"
    assert _mask_email("") == "***"
