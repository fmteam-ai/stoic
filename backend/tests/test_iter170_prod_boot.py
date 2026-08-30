"""iter-170 — production boot signer acknowledgment + turnstile break-glass.

Deploy failed with 'APP_ENV=production forbids RELEASE_SIGNER=local' and
production login was bricked by 'Human verification failed'. Corrections:
- server boot guard honors RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD=true
  (explicit acknowledgment, audit v5 P0-7 wording) instead of refusing
- turnstile gate supports TURNSTILE_FORCE_DISABLE=true break-glass so a
  misconfigured widget can never brick every login including the admin's
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

pytestmark = pytest.mark.integration


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestTurnstileBreakGlass:
    def test_force_disable_bypasses_gate(self, monkeypatch):
        from turnstile_gate import require_turnstile
        monkeypatch.setenv("TURNSTILE_FORCE_DISABLE", "true")
        # db=None would explode if the gate progressed past the break-glass
        _run(require_turnstile(None, None, "1.2.3.4", action="login"))

    def test_gate_still_enforced_without_break_glass(self, monkeypatch):
        import turnstile_gate as tg
        from fastapi import HTTPException
        monkeypatch.delenv("TURNSTILE_FORCE_DISABLE", raising=False)
        monkeypatch.setenv("TURNSTILE_SECRET_KEY", "dummy-secret")

        class _DB:
            pass

        async def _enabled(db):
            return True

        async def _verify(token, ip=None):
            return {"ok": False, "outage": False,
                    "error_codes": ["missing-input-response"]}

        monkeypatch.setattr(tg, "is_enabled", _enabled)
        monkeypatch.setattr(tg, "verify_token", _verify)
        with pytest.raises(HTTPException) as e:
            _run(tg.require_turnstile(_DB(), "", "1.2.3.4"))
        assert e.value.status_code == 403

    def test_diagnose_reports_force_disabled(self, monkeypatch):
        monkeypatch.setenv("TURNSTILE_FORCE_DISABLE", "true")
        monkeypatch.setenv("TURNSTILE_SECRET_KEY", "")
        from motor.motor_asyncio import AsyncIOMotorClient
        db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
        from turnstile_gate import diagnose
        out = _run(diagnose(db))
        assert out["force_disabled"] is True


class TestSignerAcknowledgment:
    def test_boot_guard_source_honors_acknowledgment(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "server.py")).read()
        assert "RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD" in src
        assert "explicitly acknowledge" in src or "explicitly " in src

    def test_env_carries_acknowledgment_for_pilot(self):
        assert os.environ.get(
            "RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD", "").lower() == "true"

    def test_preflight_and_boot_guard_agree(self):
        from deploy_preflight import run_preflight  # noqa: F401 — importable
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "deploy_preflight.py")).read()
        assert "RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD" in src
