"""Publish Secrets panel cannot save an empty value — `disabled` must read as absent."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def test_disabled_sentinel_reads_as_absent(monkeypatch):
    from app_env import bypass_token
    for v in ("disabled", "DISABLED", " unset ", "none", "off", "-", ""):
        monkeypatch.setenv("RATE_LIMIT_BYPASS_TOKEN", v)
        assert bypass_token("RATE_LIMIT_BYPASS_TOKEN") == ""
    monkeypatch.setenv("RATE_LIMIT_BYPASS_TOKEN", "real-value")
    assert bypass_token("RATE_LIMIT_BYPASS_TOKEN") == "real-value"
    monkeypatch.delenv("RATE_LIMIT_BYPASS_TOKEN")
    assert bypass_token("RATE_LIMIT_BYPASS_TOKEN") == ""


def test_sentinel_is_never_a_usable_bypass_header(monkeypatch):
    import asyncio
    from fastapi import Request
    from security import rate_limit
    monkeypatch.setenv("RATE_LIMIT_BYPASS_TOKEN", "disabled")
    monkeypatch.setenv("APP_ENV", "preview")
    req = Request({"type": "http", "method": "POST", "path": "/", "query_string": b"", "client": ("9.9.9.9", 1),
                   "headers": [(b"x-ratelimit-bypass", b"disabled")]})

    class _Coll:
        async def find_one_and_update(self, *a, **k):
            return {"n": 10 ** 6}

    class _DB:
        rate_limits = _Coll()

        def __getattr__(self, name):
            return _Coll()
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        asyncio.run(rate_limit(_DB(), "unit", "k", 1, 60, "limited", request=req))
    assert e.value.status_code == 429


def test_production_boot_guard_accepts_disabled_sentinel():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server.py")).read()
    assert 'bypass_token("STEP_UP_BYPASS_TOKEN") or bypass_token("RATE_LIMIT_BYPASS_TOKEN")' in src
    for f in ("step_up.py", "security.py", "deploy_preflight.py"):
        assert "bypass_token(" in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", f)).read(), f


def test_ed25519_disabled_sentinel_is_absent_for_external_signer():
    from release_signing import signer_config_violations
    env = {"RELEASE_SIGNER": "external", "ED25519_SIGNING_KEY_B64": "disabled",
           "RELEASE_SIGNER_URL": "https://signer.example.fly.dev/sign",
           "RELEASE_SIGNER_ALLOWED_HOSTS": "signer.example.fly.dev", "RELEASE_SIGNER_TOKEN": "t" * 32,
           "APP_ENV": "production"}
    assert not any("ED25519_SIGNING_KEY_B64" in v for v in signer_config_violations(env))
    env["ED25519_SIGNING_KEY_B64"] = "QUFB" * 11
    assert any("ED25519_SIGNING_KEY_B64" in v for v in signer_config_violations(env))
