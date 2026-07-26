"""iter-131 — Email OTP login gate + admin toggle + sender name.

Covers: disabled no-op, challenge issue + resend cooldown, wrong-code
attempt limiting, expiry, success single-use, TOTP skip (gate not called
for 2FA users — verified structurally in auth_routes), sender name format.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

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


@pytest.fixture()
def otp_env(monkeypatch):
    """Enabled OTP + captured emails + a throwaway user. Cleans up after."""
    import email_sender
    sent = []

    async def _fake_send(recipient, subject, html, text=None, sender=None):
        sent.append({"to": recipient, "subject": subject, "html": html})
        return {"ok": True, "id": "fake"}

    monkeypatch.setattr(email_sender, "send_email", _fake_send)

    from bson import ObjectId
    import login_otp as lo
    db = _db()
    uid = ObjectId()
    user = {"_id": uid, "email": f"otp-{uid}@test.local", "name": "Otp Tester"}

    async def _setup():
        await lo.set_enabled(db, True, actor_email="test")
    _run(_setup())

    yield {"db": db, "user": user, "sent": sent, "lo": lo}

    async def _teardown():
        await lo.set_enabled(db, False, actor_email="test")
        await db.login_otps.delete_many({"user_id": str(uid)})
    _run(_teardown())


def test_gate_noop_when_disabled():
    import login_otp as lo
    from bson import ObjectId
    db = _db()

    async def _t():
        await lo.set_enabled(db, False)
        # must not raise, must not need email
        await lo.otp_gate(db, {"_id": ObjectId(), "email": "x@y.z"}, None)
    _run(_t())


def test_challenge_issued_and_resend_cooldown(otp_env):
    db, user, sent, lo = (otp_env["db"], otp_env["user"],
                          otp_env["sent"], otp_env["lo"])

    async def _t():
        with pytest.raises(HTTPException) as e1:
            await lo.otp_gate(db, user, None)
        assert e1.value.detail["code"] == "email_otp_sent"
        assert len(sent) == 1
        assert user["email"] == sent[0]["to"]
        assert "sign-in code" in sent[0]["subject"]

        # immediate retry without code → cooldown, NO second email
        with pytest.raises(HTTPException) as e2:
            await lo.otp_gate(db, user, None)
        assert e2.value.detail["code"] == "email_otp_sent"
        assert 0 < e2.value.detail["resend_in"] <= lo.OTP_RESEND_COOLDOWN_SECONDS
        assert len(sent) == 1

        # cooldown elapsed → fresh code emailed
        await db.login_otps.update_one(
            {"user_id": str(user["_id"])},
            {"$set": {"last_sent_at": (
                datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()}})
        with pytest.raises(HTTPException):
            await lo.otp_gate(db, user, None)
        assert len(sent) == 2
    _run(_t())


def test_wrong_code_attempts_then_lockout(otp_env):
    db, user, lo = otp_env["db"], otp_env["user"], otp_env["lo"]

    async def _t():
        with pytest.raises(HTTPException):
            await lo.otp_gate(db, user, None)  # issue
        for i in range(lo.OTP_MAX_ATTEMPTS - 1):
            with pytest.raises(HTTPException) as e:
                await lo.otp_gate(db, user, "000001")
            assert e.value.detail["code"] == "invalid_email_otp"
            assert e.value.detail["attempts_left"] == lo.OTP_MAX_ATTEMPTS - 1 - i
        # 5th wrong attempt kills the code
        with pytest.raises(HTTPException) as e:
            await lo.otp_gate(db, user, "000001")
        assert e.value.detail["code"] == "email_otp_expired"
        assert await db.login_otps.find_one({"user_id": str(user["_id"])}) is None
    _run(_t())


def test_correct_code_passes_and_is_single_use(otp_env):
    db, user, lo = otp_env["db"], otp_env["user"], otp_env["lo"]

    async def _t():
        with pytest.raises(HTTPException):
            await lo.otp_gate(db, user, None)  # issue
        # plant a known code hash (code itself is never stored)
        code = "424242"
        await db.login_otps.update_one(
            {"user_id": str(user["_id"])},
            {"$set": {"code_hash": lo._hash(str(user["_id"]), code)}})
        await lo.otp_gate(db, user, code)  # passes silently
        assert await db.login_otps.find_one({"user_id": str(user["_id"])}) is None
        # replay is rejected as expired
        with pytest.raises(HTTPException) as e:
            await lo.otp_gate(db, user, code)
        assert e.value.detail["code"] == "email_otp_expired"
    _run(_t())


def test_expired_code_rejected(otp_env):
    db, user, lo = otp_env["db"], otp_env["user"], otp_env["lo"]

    async def _t():
        with pytest.raises(HTTPException):
            await lo.otp_gate(db, user, None)
        await db.login_otps.update_one(
            {"user_id": str(user["_id"])},
            {"$set": {"expires_at": (
                datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}})
        with pytest.raises(HTTPException) as e:
            await lo.otp_gate(db, user, "424242")
        assert e.value.detail["code"] == "email_otp_expired"
    _run(_t())


def test_email_failure_blocks_with_502(otp_env, monkeypatch):
    db, user, lo = otp_env["db"], otp_env["user"], otp_env["lo"]
    import email_sender

    async def _fail(recipient, subject, html, text=None, sender=None):
        return {"ok": False, "error": "sandbox"}
    monkeypatch.setattr(email_sender, "send_email", _fail)

    async def _t():
        with pytest.raises(HTTPException) as e:
            await lo.otp_gate(db, user, None)
        assert e.value.status_code == 502
        assert e.value.detail["code"] == "email_otp_send_failed"
        assert await db.login_otps.find_one({"user_id": str(user["_id"])}) is None
    _run(_t())


def test_issue_volume_cap(otp_env):
    db, user, lo = otp_env["db"], otp_env["user"], otp_env["lo"]

    async def _t():
        for _ in range(6):
            with pytest.raises(HTTPException) as e:
                await lo.otp_gate(db, user, None)
            assert e.value.detail["code"] == "email_otp_sent"
            await db.login_otps.update_one(
                {"user_id": str(user["_id"])},
                {"$set": {"last_sent_at": (
                    datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()}})
        with pytest.raises(HTTPException) as e:
            await lo.otp_gate(db, user, None)
        assert e.value.status_code == 429
    _run(_t())


def test_verify_volume_cap(otp_env):
    db, user, lo = otp_env["db"], otp_env["user"], otp_env["lo"]

    async def _t():
        for _round in range(2):  # 2 codes × 5 wrong = 10 recorded failures
            with pytest.raises(HTTPException):
                await lo.otp_gate(db, user, None)  # issue
            for _ in range(5):
                with pytest.raises(HTTPException):
                    await lo.otp_gate(db, user, "999999")
        with pytest.raises(HTTPException) as e:
            await lo.otp_gate(db, user, "999999")
        assert e.value.status_code == 429
    _run(_t())


def test_admin_role_exempt(otp_env):
    db, lo = otp_env["db"], otp_env["lo"]
    from bson import ObjectId

    async def _t():
        # must return silently, no email, no challenge doc
        admin = {"_id": ObjectId(), "email": "adm@test.local", "role": "admin"}
        await lo.otp_gate(db, admin, None)
        assert await db.login_otps.find_one({"user_id": str(admin["_id"])}) is None
        assert otp_env["sent"] == []
    _run(_t())


def test_totp_users_skip_email_otp_structurally():
    """The gate only runs on the non-2FA branch of /auth/login."""
    import inspect
    from routes import auth_routes
    src = inspect.getsource(auth_routes.login)
    idx_2fa = src.index('user.get("two_factor_enabled")')
    idx_gate = src.index("otp_gate")
    assert idx_gate > idx_2fa
    assert "else:" in src[idx_2fa:idx_gate]


def test_sender_name_format():
    import importlib
    import email_sender
    importlib.reload(email_sender)
    if os.environ.get("SENDER_NAME"):
        assert email_sender._SENDER.startswith(os.environ["SENDER_NAME"] + " <")
        assert email_sender._SENDER.endswith(">")
    else:
        assert "<" not in email_sender._SENDER


def test_admin_toggle_roundtrip():
    import login_otp as lo
    db = _db()

    async def _t():
        before = await lo.is_enabled(db)
        try:
            await lo.set_enabled(db, True, actor_email="test@x.y")
            assert await lo.is_enabled(db) is True
            await lo.set_enabled(db, False, actor_email="test@x.y")
            assert await lo.is_enabled(db) is False
        finally:
            await lo.set_enabled(db, before)
    _run(_t())
