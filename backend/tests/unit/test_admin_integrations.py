"""Admin → Integrations: sealed vault, registry, re-auth gate, lazy provider reads."""
import os
import pytest

import integrations_settings as integ

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _master(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "unit-test-secret")
    monkeypatch.delenv("SECRETS_MASTER_KEY", raising=False)


def test_seal_unseal_roundtrip_and_tamper_detection():
    doc = integ.seal("dummy_secret_1234567890")
    assert integ.unseal(doc) == "dummy_secret_1234567890"
    assert doc["master_key_id"] == integ.master_key_id() and len(doc["master_key_id"]) == 12
    bad = dict(doc); bad["ciphertext"] = doc["ciphertext"][:-4] + "AAAA"
    with pytest.raises(Exception):
        integ.unseal(bad)


def test_master_key_from_env_must_be_32_bytes(monkeypatch):
    import base64
    monkeypatch.setenv("SECRETS_MASTER_KEY", base64.b64encode(b"x" * 16).decode())
    with pytest.raises(RuntimeError):
        integ.master_key_id()
    monkeypatch.setenv("SECRETS_MASTER_KEY", base64.b64encode(b"y" * 32).decode())
    assert integ.unseal(integ.seal("v")) == "v"


def test_tail_never_leaks_more_than_four_chars():
    assert integ.tail("dummy_secret_ABCDEFGH1234") == "…1234"
    assert integ.tail("short") == "set" and integ.tail("") == ""


def test_registry_covers_the_three_requested_integrations_plus_ai():
    provs = {v[0] for v in integ.REGISTRY.values()}
    assert provs == {"stripe", "turnstile", "email", "ai"} == set(integ.PROVIDERS)
    assert integ.REGISTRY["STRIPE_API_KEY"][1] and integ.REGISTRY["TURNSTILE_SECRET_KEY"][1] and integ.REGISTRY["RESEND_API_KEY"][1]
    assert not integ.REGISTRY["TURNSTILE_SITE_KEY"][1]          # public value shown in clear


def test_secret_route_requires_admin_and_reauth_and_audits():
    src = open(os.path.join(BACKEND, "routes", "admin_routes.py")).read()
    blk = src[src.index('@router.post("/admin/integrations/secret")'):]
    assert "require_admin(user)" in blk and "await _reauth(db, user" in blk
    re = src[src.index("async def _reauth("):src.index('@router.post("/admin/integrations/secret")')]
    assert "verify_password(" in re and "verify_code_once(" in re and 'rate_limit(db, "admin_reauth"' in re
    mod = open(integ.__file__).read()
    assert "append_chained(db" in mod and '"action": "integration_secret_update"' in mod


def test_providers_read_credentials_lazily_so_vault_updates_apply_without_restart():
    es = open(os.path.join(BACKEND, "email_sender.py")).read()
    assert "def _api_key()" in es and "_API_KEY = os.environ" not in es
    assert "load_vault_sync" in open(os.path.join(BACKEND, "server.py")).read()
    assert "load_vault_sync" in open(os.path.join(BACKEND, "workers", "base.py")).read()
