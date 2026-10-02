"""Audit v2 P2-02 — plaintext bridge tokens: release-readiness BLOCKER while
any remain, production recommendation once none remain, and account
listings/exports never carry bridge_token* fields. Pure fakes — no Mongo."""
import asyncio
import inspect
import logging

import pytest
from bson import ObjectId

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.run(coro)


def _match(doc, q):
    for k, cond in q.items():
        if k == "$or":
            if not any(_match(doc, s) for s in cond):
                return False
            continue
        val = doc.get(k)
        if isinstance(cond, dict):
            for op, arg in cond.items():
                if op == "$type" and not (arg == "string" and isinstance(val, str)):
                    return False
                if op == "$ne" and val == arg:
                    return False
        elif val != cond:
            return False
    return True


class Accounts:
    def __init__(self, docs):
        self.docs = docs

    async def count_documents(self, q):
        return sum(1 for d in self.docs if _match(d, q))


class DB:
    def __init__(self, docs):
        self.accounts = Accounts(docs)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    import auth
    monkeypatch.delenv("BRIDGE_TOKEN_PLAINTEXT_FALLBACK", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setattr(auth, "_fallback_recommendation_logged", False)


DOCS = [
    {"_id": 1, "bridge_token": "plain-1"},
    {"_id": 2, "bridge_token_hash": "h", "bridge_token_prev": "plain-prev"},
    {"_id": 3, "bridge_token_hash": "h3"},
    {"_id": 4, "bridge_token": None, "bridge_token_hash": "h4"},
    {"_id": 5, "bridge_token": "", "bridge_token_hash": "h5"},
]


def test_readiness_blocker_while_plaintext_remains():
    from auth import bridge_plaintext_readiness
    out = _run(bridge_plaintext_readiness(DB(DOCS)))
    assert out["ok"] is False
    assert out["severity"] == "blocker"
    assert out["plaintext_accounts"] == 2
    assert out["remediation"] == "python -m migrations.hash_bridge_tokens"
    assert "python -m migrations.hash_bridge_tokens" in out["note"]


def test_readiness_ok_when_no_plaintext_and_no_prod_warning_in_preview(caplog):
    from auth import bridge_plaintext_readiness
    with caplog.at_level(logging.WARNING):
        out = _run(bridge_plaintext_readiness(DB(DOCS[2:])))
    assert out["ok"] is True and out["plaintext_accounts"] == 0
    assert "recommendation" not in out
    assert "BRIDGE_TOKEN_PLAINTEXT_FALLBACK" not in caplog.text


def test_production_zero_plaintext_logs_fallback_recommendation_once(monkeypatch, caplog):
    from auth import bridge_plaintext_readiness
    monkeypatch.setenv("APP_ENV", "production")
    with caplog.at_level(logging.WARNING):
        out = _run(bridge_plaintext_readiness(DB(DOCS[2:])))
        _run(bridge_plaintext_readiness(DB(DOCS[2:])))
    assert out["ok"] is True
    assert "BRIDGE_TOKEN_PLAINTEXT_FALLBACK=false" in out["recommendation"]
    assert caplog.text.count("BRIDGE_TOKEN_PLAINTEXT_FALLBACK=false") == 1


def test_production_fallback_already_off_no_recommendation(monkeypatch):
    from auth import bridge_plaintext_readiness
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("BRIDGE_TOKEN_PLAINTEXT_FALLBACK", "false")
    out = _run(bridge_plaintext_readiness(DB(DOCS[2:])))
    assert out["ok"] is True and "recommendation" not in out


def test_fallback_stays_default_on():
    from auth import bridge_plaintext_fallback_enabled
    assert bridge_plaintext_fallback_enabled() is True


def test_release_readiness_wires_the_check():
    import routes.ops_routes as ops
    src = inspect.getsource(ops.release_readiness)
    assert 'checks["bridge_token_plaintext"] = await bridge_plaintext_readiness(db)' in src


def test_account_serialization_strips_every_bridge_token_field():
    import routes.account_routes as ar
    doc = {"_id": ObjectId(), "user_id": "u", "creds": {},
           "bridge_token": "plain", "bridge_token_prev": "plain-prev",
           "bridge_token_hash": "h", "bridge_token_prev_hash": "ph",
           "bridge_token_last4": "lain", "bridge_token_prev_expires": "x",
           "bridge_token_revoked_at": "y"}
    out = ar._serialize(doc)
    assert not [k for k in out if k.startswith("bridge_token")]
    assert out["has_bridge_token"] is True
    assert "plain" not in str(out)


def test_account_serialization_hash_only_still_reports_token():
    import routes.account_routes as ar
    out = ar._serialize({"_id": ObjectId(), "bridge_token_hash": "h"})
    assert out["has_bridge_token"] is True
    assert not [k for k in out if k.startswith("bridge_token")]
    out2 = ar._serialize({"_id": ObjectId()})
    assert out2["has_bridge_token"] is False


def test_strip_helper():
    from auth import strip_bridge_token_fields
    d = {"a": 1, "bridge_token": "x", "bridge_token_hash": "y"}
    assert strip_bridge_token_fields(d) == {"a": 1}
