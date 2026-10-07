"""Security Telegram keys are vault keys (Admin → Integrations): sealed set/clear, validation, worker refresh
without restart, status/readiness aware of the vault source; I2 requires the hashed bridge-token index."""
import asyncio
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
TOKEN = "8646170133:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
ENV = {"JWT_SECRET": "unit-test-jwt-secret-for-vault", "APP_ENV": "preview"}


def _read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield d
        return gen()


class _Vault:
    def __init__(self):
        self.docs = {}

    def find(self, flt, *_a, **_k):
        ids = flt["_id"]["$in"]
        return _Cursor([{"_id": k, **v} for k, v in self.docs.items() if k in ids])

    async def find_one(self, flt, *_a, **_k):
        d = self.docs.get(flt["_id"])
        return {"_id": flt["_id"], **d} if d else None

    async def update_one(self, flt, upd, upsert=False):
        self.docs[flt["_id"]] = {**self.docs.get(flt["_id"], {}), **upd["$set"]}

    async def delete_one(self, flt):
        self.docs.pop(flt["_id"], None)


class _DB:
    def __init__(self):
        self.secrets_vault = _Vault()
        self.platform_state = _Vault()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_registry_validators_and_set_clear_refresh_cycle():
    import integrations_settings as integ
    for k in integ.SECURITY_TELEGRAM_KEYS:
        assert integ.REGISTRY[k][0] == "security_telegram"
    assert integ.PROVIDERS["security_telegram"]
    db = _DB()
    chained = []

    async def fake_chain(_db, entry, collection="admin_audit_log"):
        chained.append(entry)

    clean = {k: v for k, v in os.environ.items() if not k.startswith("SECURITY_AGENT_TELEGRAM")}
    with patch.dict(os.environ, {**clean, **ENV}, clear=True), patch("audit_chain.append_chained", fake_chain), \
            patch.object(integ, "_dotenv_value", lambda _k: None):
        with pytest.raises(ValueError):
            _run(integ.update_secret(db, "SECURITY_AGENT_TELEGRAM_BOT_TOKEN", "bot8646170133:AAH", {"email": "a@x"}))
        with pytest.raises(ValueError):
            _run(integ.update_secret(db, "SECURITY_AGENT_TELEGRAM_CHAT_ID", "abc", {"email": "a@x"}))
        res = _run(integ.update_secret(db, "SECURITY_AGENT_TELEGRAM_BOT_TOKEN", TOKEN, {"email": "a@x"}))
        assert res["configured"] and TOKEN not in str(res) and res["display"] == "…xxxx"
        _run(integ.update_secret(db, "SECURITY_AGENT_TELEGRAM_CHAT_ID", "981306515", {"email": "a@x"}))
        assert os.environ["SECURITY_AGENT_TELEGRAM_BOT_TOKEN"] == TOKEN
        assert "ciphertext" in db.secrets_vault.docs["SECURITY_AGENT_TELEGRAM_BOT_TOKEN"]
        assert TOKEN not in str(db.secrets_vault.docs) and TOKEN not in str(chained)
        # a second process (worker) that booted without the keys picks them up on refresh
        os.environ.pop("SECURITY_AGENT_TELEGRAM_BOT_TOKEN")
        os.environ.pop("SECURITY_AGENT_TELEGRAM_CHAT_ID")
        assert _run(integ.refresh_keys(db)) == 2
        from security_agent.alerts import telegram_creds
        assert telegram_creds() == (TOKEN, "981306515")
        # cleared in the vault → the worker drops it on the next refresh
        _run(integ.update_secret(db, "SECURITY_AGENT_TELEGRAM_BOT_TOKEN", "", {"email": "a@x"}))
        assert "SECURITY_AGENT_TELEGRAM_BOT_TOKEN" not in os.environ
        os.environ["SECURITY_AGENT_TELEGRAM_BOT_TOKEN"] = TOKEN   # stale copy in another process
        integ._VAULT_LOADED["SECURITY_AGENT_TELEGRAM_BOT_TOKEN"] = TOKEN
        assert _run(integ.refresh_keys(db)) == 1
        assert telegram_creds() is None


def test_status_readiness_and_test_button_see_the_vault_source():
    import integrations_settings as integ
    import security_alert_test as sat
    import demo_readiness as dr
    db = _DB()
    db.secrets_vault.docs["SECURITY_AGENT_TELEGRAM_BOT_TOKEN"] = {"ciphertext": "x", "nonce": "y"}
    env = {**ENV, "SECURITY_AGENT_TELEGRAM_BOT_TOKEN": TOKEN, "SECURITY_AGENT_TELEGRAM_CHAT_ID": "981306515"}
    with patch.dict(os.environ, env):
        st = _run(sat.status(db))
        assert st["configured"] and st["token_source"] == "vault" and st["chat_id_masked"] == "98…515"
        checks = {c["id"]: c for c in dr.env_checks(vault_token=True)}
        assert checks["telegram_secret"]["status"] == "pass" and "vault" in checks["telegram_secret"]["detail"]
        assert checks["telegram_chat"]["status"] == "pass"
    with patch.dict(os.environ, {k: v for k, v in ENV.items()}, clear=False):
        os.environ.pop("SECURITY_AGENT_TELEGRAM_BOT_TOKEN", None)
        checks = {c["id"]: c for c in dr.env_checks(vault_token=False)}
        assert checks["telegram_secret"]["status"] == "fail" and "Integrations" in checks["telegram_secret"]["hint"]
        st = _run(sat.status(_DB()))
        assert not st["configured"] and "SET" in st["hint"]
    src = _read("backend/integrations_settings.py")
    assert 'if provider == "security_telegram":' in src and "sat.send_test" in src
    # workers refresh the keys every cycle (no restart)
    assert "refresh_keys" in _read("backend/alerting.py") and "refresh_keys" in _read("backend/security_agent/runner.py")
    page = _read("frontend/src/pages/AdminIntegrations.jsx")
    assert 'keysOf(data, "security_telegram")' in page
    card = _read("frontend/src/components/admin/SecurityAlertsCard.jsx")
    assert "KeyRow" in card and "onEdit" in card


def test_i2_requires_the_hashed_bridge_token_index():
    from security_agent.checks import integrity_platform_trading as I
    assert I.REQUIRED_INDEXES["accounts"] == "bridge_token_hash_1"
    assert "bridge_token_hash" in _read("backend/bridge_tokens.py")
