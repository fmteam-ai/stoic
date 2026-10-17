"""Phase 3 — rules-based signed policy + automatic admission (Simple Onboarding Spec U12 U13 U14 O4)."""
import asyncio
import base64
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
KEY = Ed25519PrivateKey.generate()
AUDIT: list = []
PUB_B64 = base64.b64encode(KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8").read()


@pytest.fixture
def signer(monkeypatch):
    import release_signing as rs
    import rules_policy as rp
    from release_signing import domain_bytes
    monkeypatch.setattr(rp, "_now", lambda: datetime.now(timezone.utc))
    real_verify = rs.verify_hex

    def _verify(data, sig, pub_b64=None, *, purpose):
        return real_verify(data, sig, PUB_B64, purpose=purpose)
    monkeypatch.setattr(rs, "verify_hex", _verify)
    monkeypatch.setenv("APP_ENV", "preview")
    monkeypatch.delenv("STOIC_INSTALLATION_ID", raising=False)

    AUDIT.clear()

    async def _audit(db, entry, **k):
        AUDIT.append(entry)
    import audit_chain
    monkeypatch.setattr(audit_chain, "append_chained", _audit)

    def make(**over):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        pol = {"schema": rp.SCHEMA, "installation_id": "dev-local", "environment": "preview", "policy_version": "demo-rules-v1",
               "demo_only": True, "max_accounts_per_user": 1, "allowed_brokers": ["*"], "risk_caps": {"max_risk_pct": 1.0},
               "reason": "migrate demo-1x1-v1 to rules", "issuer": "ops@x", "issued_at": now.isoformat(),
               "expires_at": (now + timedelta(days=30)).isoformat(), "nonce": secrets.token_hex(8)}
        pol.update(over)
        pol["signature_hex"] = KEY.sign(domain_bytes(rp.PURPOSE, rp.body(pol))).hex()
        return pol
    return make


def test_u12_schema_signature_and_limits(signer):
    import rules_policy as rp
    assert rp.problems(signer()) == []
    bad = signer(); bad["max_accounts_per_user"] = 5                       # tampered after signing
    assert any("signature" in e for e in rp.problems(bad))
    assert any("1..50" in e for e in rp.problems(signer(max_accounts_per_user=0)))
    assert any("1..45 days" in e for e in rp.problems(signer(expires_at=(datetime.now(timezone.utc) + timedelta(days=60)).isoformat())))
    assert any("environment" in e for e in rp.problems(signer(environment="production")))
    assert any("allowed_brokers" in e for e in rp.problems(signer(allowed_brokers=[])))
    from release_signing import PURPOSES, RELEASE_PURPOSES
    assert "policy-rules" in PURPOSES and "policy-rules" in RELEASE_PURPOSES    # CI/operator key only, never the API bundle key


def test_u13_activation_without_release_upload_or_repo(signer, tmp_path, monkeypatch):
    import rules_policy as rp
    db = FakeDb()
    pol = signer()
    doc = _run(rp.activate(db, pol, actor="admin@x", source="upload"))
    assert doc["_id"] == rp.STATE_ID and _run(rp.active(db))["policy_version"] == "demo-rules-v1"
    assert AUDIT[-1]["action"] == "rules_policy_activated"
    with pytest.raises(HTTPException) as ei:
        _run(rp.activate(db, pol, actor="admin@x", source="upload"))       # nonce replay
    assert ei.value.detail["code"] == "rules_policy_replayed"
    # repo loader: newest valid file wins, invalid files are skipped, nothing happens when already current
    import json
    monkeypatch.setattr(rp, "POLICIES_DIR", str(tmp_path))
    newer = signer(policy_version="demo-rules-v2", issued_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
                   expires_at=(datetime.now(timezone.utc) + timedelta(days=31)).isoformat())
    (tmp_path / "demo-rules-v2.json").write_text(json.dumps(newer))
    (tmp_path / "broken.json").write_text("{not json")
    tampered = signer(policy_version="evil"); tampered["demo_only"] = False
    (tmp_path / "evil.json").write_text(json.dumps(tampered))
    out = _run(rp.load_from_repo(db))
    assert out["policy_version"] == "demo-rules-v2"
    assert _run(rp.load_from_repo(db)) is None
    src = _read("backend", "server.py")
    assert "await _rp.load_from_repo(get_db())" in src
    routes = _read("backend", "routes", "authority_routes.py")
    assert '@router.post("/policy/upload")' in routes and '@router.post("/admission/{account_id}/approve")' in routes


def _acc(**kw):
    base = {"_id": ObjectId(), "user_id": "u1", "mode": "live", "label": "demo", "broker": "OnEquity", "account_number": "1083440",
            "trading_enabled": True, "account_type": "demo", "broker_environment": "DEMO"}
    base.update(kw)
    return base


def test_u12_admission_rules(signer, monkeypatch):
    import rules_policy as rp
    import broker_env
    pol = signer()
    monkeypatch.setattr(broker_env, "attested_environment", lambda a: a.get("_env", "DEMO"))
    ok = rp.evaluate_admission(_acc(), pol, user_account_count=1)
    assert ok["state"] == "admitted" and all(ok["checks"].values())
    assert rp.evaluate_admission(_acc(), pol, user_account_count=2)["state"] == "blocked"                   # cap
    assert rp.evaluate_admission(_acc(broker="NoSuchBroker"), pol, user_account_count=1)["state"] == "blocked"
    assert rp.evaluate_admission(_acc(_env="LIVE"), pol, user_account_count=1)["state"] == "blocked"       # demo-only policy
    assert rp.evaluate_admission(_acc(), pol, user_account_count=1, bot_cfg={"risk_per_trade_pct": 2.0})["state"] == "blocked"
    live_pol = signer(demo_only=False, expires_at=(datetime.now(timezone.utc) + timedelta(days=40)).isoformat())
    real = rp.evaluate_admission(_acc(_env="LIVE"), live_pol, user_account_count=1)
    assert real["state"] == "pending_operator" and "operator approval" in real["reason"]                     # real money needs a human
    real_reported = rp.evaluate_admission(_acc(account_trade_mode="real", ea_identity={"trade_mode": "real"}), pol, user_account_count=1)
    assert real_reported["state"] != "admitted"                                                             # broker truth wins
    assert rp.evaluate_admission(_acc(), {**pol, "expired": True}, user_account_count=1)["state"] == "blocked"
    assert rp.broker_allowed({"allowed_brokers": ["ICMarkets"]}, "icmarkets") and not rp.broker_allowed({"allowed_brokers": ["ICMarkets"]}, "OnEquity")


def test_u12_sync_and_operator_approval(signer, monkeypatch):
    import rules_policy as rp
    import broker_env
    db = FakeDb()
    monkeypatch.setattr(broker_env, "attested_environment", lambda a: a.get("_env", "DEMO"))
    assert _run(rp.sync_admission(db, _acc())) is None                                   # no policy → nothing (fail closed)
    _run(rp.activate(db, signer(demo_only=False, expires_at=(datetime.now(timezone.utc) + timedelta(days=40)).isoformat()),
                     actor="admin@x", source="upload"))
    demo = _acc(); _run(db.accounts.insert_one(dict(demo)))
    adm = _run(rp.sync_admission(db, demo))
    assert adm["state"] == "admitted" and adm["by"] == rp.SYSTEM_ACTOR and AUDIT[-1]["action"] == "account_admission_admitted"
    assert _run(rp.sync_admission(db, demo)) == adm                                      # idempotent, no second audit
    real = _acc(_env="LIVE", user_id="u2", account_number="7"); _run(db.accounts.insert_one(dict(real)))
    assert _run(rp.sync_admission(db, real))["state"] == "pending_operator"
    approved = _run(rp.operator_approve(db, real, approver_email="ops@x"))
    assert approved["state"] == "admitted" and approved["by"] == "ops@x"
    real["admission"] = approved
    assert _run(rp.sync_admission(db, real))["by"] == "ops@x"                             # operator decision kept
    with pytest.raises(HTTPException):
        _run(rp.operator_approve(db, _acc(user_id="u3", broker="Nope"), approver_email="ops@x"))   # blocked stays blocked
    hb = _read("backend", "routes", "bridge_routes.py").split('@router.post("/heartbeat")')[1]
    assert "await _rp.sync_admission(db, _fresh)" in hb


def test_u14_inventory_auto_approved_under_rules(signer, monkeypatch):
    import inventory_projection as ip
    import rules_policy as rp
    import broker_env
    monkeypatch.setattr(broker_env, "attested_environment", lambda a: "DEMO")
    db = FakeDb()
    _run(rp.activate(db, signer(), actor="admin@x", source="repo"))
    acc = _acc(admission={"state": "admitted", "by": rp.SYSTEM_ACTOR, "policy_version": "demo-rules-v1"})
    _run(db.accounts.insert_one(dict(acc)))
    _run(db.bot_configs.insert_one({"user_id": "u1", "account_id": str(acc["_id"]), "active": True}))
    proj = _run(ip.projection(db, None))
    assert proj["violations"] == [] and proj["approvable"] is True and proj["blocking"] is False
    # M3 (main127) — the GET projection is read-only: nothing written until the explicit write step runs
    assert proj["auto_approval_due"] is True and _run(db.inventory_config_events.count_documents({})) == 0
    assert _run(ip.auto_approve_under_rules(db)) is not None
    ev = _run(db.inventory_config_events.find_one({"approved": True}))
    assert ev["actor"] == rp.SYSTEM_ACTOR and ev["approval_mode"] == "rules_policy" and ev["inventory_hash"] == proj["inventory_hash"]
    assert any(e["action"] == "inventory_config_auto_approved" for e in AUDIT)
    assert _run(ip.auto_approve_under_rules(db)) is None                   # idempotent: same hash, nothing due
    # a second account for the same user breaches the cap → violation scoped to THAT user, no new approval event
    other = _acc(user_id="u1", account_number="2", label="second")
    _run(db.accounts.insert_one(dict(other)))
    _run(db.bot_configs.insert_one({"user_id": "u1", "account_id": str(other["_id"]), "active": True}))
    proj2 = _run(ip.projection(db, None))
    assert any("above the policy cap" in v for v in proj2["violations"]) and any("not admitted" in v for v in proj2["violations"])
    # the new account changed the hash → unapproved change (platform) until the write step records it; the per-user
    # breach itself never blocks the platform
    assert proj2["platform_violations"] == [ip.V_UNAPPROVED_CHANGE] and "u1" in proj2["per_user_blocks"]
    assert _run(ip.auto_approve_under_rules(db)) is not None and _run(db.inventory_config_events.count_documents({})) == 2
    proj2 = _run(ip.projection(db, None))
    # H1 (main127) — rule breaches never close the PLATFORM: only user u1's accounts
    assert proj2["blocking"] is False and proj2["platform_violations"] == [] and "u1" in proj2["per_user_blocks"]
    assert len(proj2["per_user_blocks"]["u1"]) == 2
    import trading_authority as ta
    monkeypatch.setattr(ip, "production_mode", lambda: False)
    # prod v1.60.23 (bug 3) — the breach closes ONLY the breaching account (the younger, beyond-cap one);
    # the owner's older admitted account and every other user stay FULL
    assert set(proj2["per_account_blocks"]) == {str(other["_id"])}
    mine = _run(ta.inventory_domain(db, {"_id": acc["_id"], "user_id": "u1"}))
    assert mine["level"] == "FULL"
    second = _run(ta.inventory_domain(db, {"_id": other["_id"], "user_id": "u1"}))
    assert second["level"] == "CLOSE_ONLY" and "this account:" in second["reason"] and "beyond the policy cap" in second["reason"]
    theirs = _run(ta.inventory_domain(db, {"_id": ObjectId(), "user_id": "u2"}))
    assert theirs["level"] == "FULL"                                      # another user is untouched
    src = _read("backend", "inventory_projection.py")
    assert "insert_one" not in src[src.index("async def projection"):src.index("async def auto_approve_under_rules")]
    for rel in (("backend", "routes", "bridge_routes.py"), ("backend", "routes", "account_routes.py"), ("backend", "routes", "bot_routes.py"), ("backend", "background_loops.py")):
        assert "auto_approve_under_rules(" in _read(*rel), rel


def test_o4_and_migration_script():
    readme = _read("policies", "README.md")
    assert "required reviewer" in readme and "stoic.policy-rules/v1" in readme
    src = _read("scripts", "sign_rules_policy.py")
    assert "--migrate-from" in src and 'sign_hex(rp.body(pol), purpose=rp.PURPOSE)' in src and "from __future__ import annotations" in src
