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


class TestTurnstileFailClosed:
    """audit round 8 P1-1 — three distinct states, fail-closed, audited break-glass."""

    def _gate(self, monkeypatch, state, action="login", policy=None, app_env="", force_disable=False):
        import turnstile_gate as tg
        from fastapi import HTTPException
        if force_disable:
            monkeypatch.setenv("TURNSTILE_FORCE_DISABLE", "true")
        else:
            monkeypatch.delenv("TURNSTILE_FORCE_DISABLE", raising=False)
        monkeypatch.delenv("TURNSTILE_BREAK_GLASS_UNTIL", raising=False)
        monkeypatch.setenv("TURNSTILE_SECRET_KEY", "dummy-secret")
        monkeypatch.setenv("APP_ENV", app_env)
        if policy:
            monkeypatch.setenv("TURNSTILE_LOGIN_DEGRADED_POLICY", policy)
        else:
            monkeypatch.delenv("TURNSTILE_LOGIN_DEGRADED_POLICY", raising=False)

        async def _enabled(db):
            return True

        async def _verify(token, ip=None, action=None):
            return {"ok": state == "ok", "state": state, "outage": state == "provider_unavailable",
                    "error_codes": [] if state == "ok" else ["x"], "hostname": None, "action": action}

        async def _no_bg(db):
            return None

        import turnstile_break_glass as tbg
        monkeypatch.setattr(tbg, "active", _no_bg)
        monkeypatch.setattr(tg, "is_enabled", _enabled)
        monkeypatch.setattr(tg, "verify_token", _verify)
        monkeypatch.setenv("TURNSTILE_SITE_KEY", "dummy-site")
        try:
            _run(tg.require_turnstile(object(), "tok", "1.2.3.4", action=action))
            return None
        except HTTPException as e:
            return e

    def test_valid_token_passes(self, monkeypatch):
        assert self._gate(monkeypatch, "ok") is None

    def test_client_fault_is_explicit_retryable_403(self, monkeypatch):
        e = self._gate(monkeypatch, "client_token_invalid")
        assert e.status_code == 403 and e.detail["code"] == "turnstile_required" and e.detail["retryable"] is True

    def test_provider_outage_fails_closed_for_every_action(self, monkeypatch):
        for action in ("login", "register", "password_reset"):
            e = self._gate(monkeypatch, "provider_unavailable", action=action)
            assert e.status_code == 503 and e.detail["code"] == "turnstile_unavailable", action

    def test_login_degraded_policy_routes_to_otp_not_bypass(self, monkeypatch):
        # require_turnstile (raise-or-pass) treats a degraded decision as DENIAL;
        # only the typed evaluate() path hands login the otp_required mode (round 9 P1-03).
        e = self._gate(monkeypatch, "provider_unavailable", action="login", policy="otp_required")
        assert e.status_code == 503 and e.detail["code"] == "turnstile_unavailable"
        # registration never degrades
        e = self._gate(monkeypatch, "provider_unavailable", action="register", policy="otp_required")
        assert e.detail["code"] == "turnstile_unavailable"

    def test_configuration_invalid_always_closed(self, monkeypatch):
        for policy in (None, "otp_required"):
            e = self._gate(monkeypatch, "configuration_invalid", policy=policy)
            assert e.status_code == 503 and e.detail["state"] == "configuration_invalid"

    def test_classify_and_hostname_action_binding(self):
        import turnstile_gate as tg
        assert tg._classify([], 502, False) == "provider_unavailable"
        assert tg._classify([], None, True) == "provider_unavailable"
        assert tg._classify(["invalid-input-secret"], None, False) == "configuration_invalid"
        assert tg._classify(["timeout-or-duplicate"], None, False) == "client_token_invalid"
        assert tg._classify(["some-new-code"], None, False) == "configuration_invalid"

    def test_force_disable_refused_in_production(self, monkeypatch):
        e = self._gate(monkeypatch, "client_token_invalid", app_env="production", force_disable=True)
        assert e is not None and e.status_code == 403          # bypass did NOT apply
        assert self._gate(monkeypatch, "client_token_invalid", app_env="preview", force_disable=True) is None

    def test_env_break_glass_retired(self):
        # round 9 P1-05: break-glass is DB-governed (turnstile_break_glass), never an env var.
        import inspect
        import turnstile_gate as tg
        src = inspect.getsource(tg)
        assert "TURNSTILE_BREAK_GLASS_UNTIL" not in src and not hasattr(tg, "_break_glass")

    def test_diagnose_reports_states(self, monkeypatch):
        monkeypatch.setenv("TURNSTILE_SECRET_KEY", "")
        from motor.motor_asyncio import AsyncIOMotorClient
        db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
        from turnstile_gate import diagnose
        out = _run(diagnose(db))
        assert out["secret_check"] == "not_configured"


class TestSignerPolicyIsOneRule:
    """audit round 8 P1-6 — preflight, boot and signer share ONE validator; no bypass."""

    def test_shared_validator(self):
        from release_signing import production_signer_violation as v
        from test_iter237_signer_round9 import EXTERNAL_OK
        assert v({"APP_ENV": "production", **EXTERNAL_OK}) is None
        # round 9 P1-01: mode alone is NOT enough — external needs its full configuration
        assert v({"APP_ENV": "production", "RELEASE_SIGNER": "external"}) is not None
        assert "forbids RELEASE_SIGNER=local" in v({"APP_ENV": "production"})
        assert "RETIRED" in v({"APP_ENV": "production", "RELEASE_SIGNER": "local", "RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD": "true"})

    def test_boot_guard_and_preflight_use_it_and_no_bypass_remains(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for f in ("server.py", "deploy_preflight.py"):
            src = open(os.path.join(root, f)).read()
            assert "production_signer_violation" in src or "signer_config_violations" in src, f
        srv = open(os.path.join(root, "server.py")).read()
        assert 'os.environ.get(\n                "RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD"' not in srv
        assert "explicitly acknowledge" not in srv
        guide = open(os.path.join(os.path.dirname(root), "docs", "SELF_HOSTING_GUIDE.md")).read()
        assert "`true` until you attach a KMS" not in guide

    def test_preflight_fails_local_even_with_retired_flag(self, monkeypatch):
        from deploy_preflight import run_preflight
        from test_iter237_signer_round9 import EXTERNAL_OK
        for k, v in {"APP_ENV": "production", "RELEASE_SIGNER": "local", "RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD": "true"}.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("RELEASE_SIGNER_DEFERRED", raising=False)
        by = {c["id"]: c for c in run_preflight()["checks"]}
        assert by["release_signer"]["status"] == "fail" and "RETIRED" in by["release_signer"]["current"]
        for k, v in EXTERNAL_OK.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD", raising=False)
        monkeypatch.delenv("ED25519_SIGNING_KEY_B64", raising=False)
        by = {c["id"]: c for c in run_preflight()["checks"]}
        assert by["release_signer"]["status"] == "pass"
