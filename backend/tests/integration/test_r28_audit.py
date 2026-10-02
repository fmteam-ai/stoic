"""Audit r28 — P1-01 identity-bound DEMO attestation · P2-01 shared versioned
pricing + fulfilment snapshot · P2-02 vault master-key policy · P2-03 durable
trial grant · P2-04 currency-neutral amounts."""
import base64
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

pytestmark = [pytest.mark.integration, pytest.mark.critical_controls]

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


# ───────────────────────────── P1-01 identity-bound attestation ─────────────────────────────

def _demo_acc(uid):
    return {"user_id": uid, "label": "r28", "mode": "live", "account_type": "demo", "broker": "VT Markets",
            "server": "VTMarkets-Demo", "broker_server": "VTMarkets-Demo", "account_number": "1289887",
            "bridge_token": f"r28-{uid}", "ea_version": "1.57", "creds_version": 0,
            "broker_account_id_reported": "1289887", "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "ea_identity": {"installation_id": "inst-A", "authoritative": True, "broker_server": "VTMarkets-Demo"}}


def test_attestation_is_void_when_any_bound_identity_field_changes():
    from broker_env import attestation_identity, attestation_state, attested_environment
    from ea_capabilities import live_gate
    acc = {**_demo_acc("u"), "_id": ObjectId()}
    att = {"environment": "DEMO", "approved_by": "adm@example.com", "at": "2026-01-01T00:00:00+00:00",
           "identity_hash": attestation_identity(acc), "proof": {"verifier": "ea_heartbeat", "proof_id": "p"}}
    bound = {**acc, "environment_attestation": att}
    assert attested_environment(bound) == "DEMO" and attestation_state(bound) == "valid" and live_gate(bound) is None
    for mutation in ({"_id": ObjectId()}, {"broker": "Other"}, {"server": "VTMarkets-Live"},
                     {"broker_server": "VTMarkets-Live"}, {"account_number": "999"},
                     {"ea_identity": {**acc["ea_identity"], "installation_id": "inst-B"}}, {"creds_version": 1},
                     {"broker_account_id_reported": "555"}):
        changed = {**bound, **mutation}
        assert attested_environment(changed) == "LIVE", mutation
        assert attestation_state(changed) == "invalidated", mutation
        assert live_gate(changed)["code"] in ("EA_DEMO_ATTESTATION_INVALIDATED", "EA_BINARY_PROOF_MISSING",
                                              "EA_RELEASE_HASH_UNPINNED"), mutation
    # an attestation copied from another record never transfers (hash includes the immutable id)
    other = {**_demo_acc("u2"), "_id": ObjectId(), "environment_attestation": att}
    assert attested_environment(other) == "LIVE"
    # legacy attestation without a digest is not honoured
    assert attested_environment({**acc, "environment_attestation": {k: v for k, v in att.items() if k != "identity_hash"}}) == "LIVE"


def test_admin_attest_binds_digest_and_credential_change_voids_it(monkeypatch):
    import routes.admin_routes as ar
    from broker_env import attestation_state
    db = _db()
    uid = str(ObjectId())
    admin = {"id": str(ObjectId()), "email": "adm@example.com", "role": "admin", "two_factor_enabled": True}
    acc_id = _run(db.accounts.insert_one(_demo_acc(uid))).inserted_id
    try:
        async def _ok(*a, **k):
            return None
        monkeypatch.setattr(ar, "_reauth", _ok)
        row = _run(ar.admin_attest_account_environment(str(acc_id), {"environment": "DEMO", "password": "x"}, admin))
        assert row["effective"] == "DEMO" and row["attestation_state"] == "valid" and len(row["identity_hash"]) == 64
        # credential rotation bumps creds_version → attestation invalidated, visible in the panel row
        _run(db.accounts.update_one({"_id": acc_id}, {"$inc": {"creds_version": 1}}))
        acc = _run(db.accounts.find_one({"_id": acc_id}))
        assert attestation_state(acc) == "invalidated"
        listing = _run(ar.admin_account_environments(admin))
        mine = next(r for r in listing["accounts"] if r["account_id"] == str(acc_id))
        assert mine["effective"] == "LIVE" and mine["attestation_state"] == "invalidated"
        # re-attest (fresh re-auth) rebinds to the new identity
        row = _run(ar.admin_attest_account_environment(str(acc_id), {"environment": "DEMO", "password": "x"}, admin))
        assert row["attestation_state"] == "valid" and row["identity_hash"] != mine["identity_hash"]
    finally:
        _run(db.accounts.delete_many({"user_id": uid}))
        _run(db.admin_audit_log.delete_many({"target_id": str(acc_id)}))


def test_account_serializer_exposes_attestation_state_not_raw_doc():
    from routes.account_routes import _serialize
    acc = {**_demo_acc("u"), "_id": ObjectId(), "environment_attestation": {"environment": "DEMO", "approved_by": "a"}}
    out = _serialize(dict(acc))
    assert out["environment"] == "DEMO" and out["environment_attested"] is False
    assert out["environment_attestation_state"] == "invalidated" and "environment_attestation" not in out


# ───────────────────────────── P2-02 vault master key ─────────────────────────────

def test_vault_requires_dedicated_master_key_in_production(monkeypatch):
    import integrations_settings as integ
    monkeypatch.setenv("JWT_SECRET", "unit-test-secret")
    monkeypatch.delenv("SECRETS_MASTER_KEY", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    assert integ.master_key_source() == "derived_from_jwt_secret" and integ.readiness_check()["ok"]
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError):
        integ._master_key()
    rc = integ.readiness_check()
    assert rc["ok"] is False and "SECRETS_MASTER_KEY" in rc["detail"]
    monkeypatch.setenv("SECRETS_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    assert integ.master_key_source() == "dedicated" and integ.readiness_check()["ok"]


def test_vault_legacy_jwt_derived_records_migrate_to_dedicated_key_at_boot(monkeypatch):
    """Upgrade path: records sealed by HKDF(JWT_SECRET) before r28 open once with the
    legacy key and are re-sealed under the dedicated key — never left undecryptable."""
    import integrations_settings as integ
    db = _db()
    key = "SENDER_NAME"
    saved = _run(db.secrets_vault.find_one({"_id": key}))
    try:
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("JWT_SECRET", "legacy-jwt-secret-for-test")
        monkeypatch.delenv("SECRETS_MASTER_KEY", raising=False)
        monkeypatch.delenv("SECRETS_MASTER_KEY_PREVIOUS", raising=False)
        legacy = {k: v for k, v in integ.seal("STOIC legacy").items() if k != "key_version"}
        _run(db.secrets_vault.replace_one({"_id": key}, {"_id": key, **legacy}, upsert=True))
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("SECRETS_MASTER_KEY", base64.b64encode(b"d" * 32).decode())
        monkeypatch.delenv(key, raising=False)
        n = integ.load_vault_sync(os.environ["MONGO_URL"], os.environ["DB_NAME"])
        assert n >= 1 and key not in integ.DECRYPT_FAILURES and os.environ[key] == "STOIC legacy"
        doc = _run(db.secrets_vault.find_one({"_id": key}))
        assert doc["key_version"] == 1 and doc["migrated_from"] == "jwt_derived"
        assert doc["master_key_id"] == integ.master_key_id() and integ.unseal(doc) == "STOIC legacy"
        # a record already on a dedicated key is NEVER opened with the legacy key
        monkeypatch.setenv("SECRETS_MASTER_KEY", base64.b64encode(b"e" * 32).decode())
        integ.load_vault_sync(os.environ["MONGO_URL"], os.environ["DB_NAME"])
        assert key in integ.DECRYPT_FAILURES
        assert integ.readiness_check()["ok"] is False
    finally:
        integ.DECRYPT_FAILURES.clear()
        monkeypatch.delenv(key, raising=False)
        if saved:
            _run(db.secrets_vault.replace_one({"_id": key}, saved, upsert=True))
        else:
            _run(db.secrets_vault.delete_one({"_id": key}))


def test_vault_decrypt_failures_are_a_readiness_blocker(monkeypatch):
    import integrations_settings as integ
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setattr(integ, "DECRYPT_FAILURES", ["STRIPE_API_KEY"])
    rc = integ.readiness_check()
    assert rc["ok"] is False and rc["undecryptable_keys"] == ["STRIPE_API_KEY"]
    st = _run(integ.status(_db()))
    assert st["keys"]["STRIPE_API_KEY"]["source"] == "vault_undecryptable"
    assert st["keys"]["STRIPE_API_KEY"]["configured"] is False and "master_key_id" not in st


# ───────────────────────────── P2-01 / P2-04 pricing ─────────────────────────────

@pytest.fixture
def pricing_snapshot():
    import plan_settings
    import subscription_plans as sp
    snap = (dict(sp.TIER_BASE_CENTS), {d: x for d, _m, x in sp.DURATION_DISCOUNTS}, sp.CURRENCY,
            sp.PRICING_VERSION, dict(plan_settings._state))
    doc = _run(_db().platform_state.find_one({"_id": plan_settings.DOC_ID}))
    yield
    sp.apply_pricing(snap[0], snap[1], snap[2], snap[3])
    plan_settings._state.update(snap[4])
    if doc:
        _run(_db().platform_state.replace_one({"_id": plan_settings.DOC_ID}, doc, upsert=True))


def test_pricing_update_bumps_shared_version_and_other_worker_catches_up(pricing_snapshot):
    import plan_settings
    import subscription_plans as sp
    db = _db()
    _run(plan_settings.load(db))
    v0 = _run(plan_settings.ensure_fresh(db))
    out = _run(plan_settings.update(db, {"base_cents": {"starter": 4900}, "currency": "eur"},
                                    {"email": "adm@example.com"}))
    assert out["pricing_version"] == v0 + 1 and sp.PRICING_VERSION == v0 + 1
    pub = sp.get_plan("starter_monthly").to_public()
    assert pub["amount_minor"] == 4900 and pub["currency"] == "eur" and pub["pricing_version"] == v0 + 1
    assert pub["effective_monthly_minor"] == 4900 and pub["savings_minor"] == 0 and pub["amount_usd"] == 49.0
    # simulate a stale replica: in-process state behind the shared document
    sp.apply_pricing({"starter": 1900}, {}, "usd", v0)
    plan_settings._state["pricing_version"] = v0
    assert sp.get_plan("starter_monthly").amount_cents == 1900
    _run(plan_settings.ensure_fresh(db))
    assert sp.PRICING_VERSION == v0 + 1 and sp.get_plan("starter_monthly").amount_cents == 4900 and sp.CURRENCY == "eur"


def test_fulfilment_refuses_price_snapshot_mismatch():
    import subscription_service as ss
    db = _db()
    uid, sid = str(ObjectId()), f"cs_r28_{ObjectId()}"
    _run(db.users.insert_one({"_id": ObjectId(uid), "email": f"{uid}@example.com", "role": "user",
                              "created_at": datetime.now(timezone.utc).isoformat()}))
    try:
        _run(ss.record_transaction(user_id=uid, user_email="x", plan_id="trader_monthly", session_id=sid,
                                   amount_usd=99.0, metadata={}, amount_cents=9900, currency="usd", pricing_version=7))
        txn = _run(db.payment_transactions.find_one({"session_id": sid}))
        assert txn["amount_minor"] == 9900 and txn["pricing_version"] == 7
        assert _run(ss.apply_successful_payment(sid, source="poll", paid_amount_minor=100, paid_currency="usd")) is None
        txn = _run(db.payment_transactions.find_one({"session_id": sid}))
        assert txn["skipped_reason"] == "price_mismatch" and txn["applied"] is False and txn["fulfilment_blocked"]
        sub = _run(db.subscriptions.find_one({"user_id": uid}))
        assert not sub or not sub.get("valid_until")
        # wrong currency with right amount is also refused; the exact snapshot is accepted
        assert _run(ss.apply_successful_payment(sid, source="poll", paid_amount_minor=9900, paid_currency="eur")) is None
        assert _run(ss.apply_successful_payment(sid, source="webhook", paid_amount_minor=9900, paid_currency="usd"))
        assert _run(db.payment_transactions.find_one({"session_id": sid}))["applied"] is True
    finally:
        _run(db.payment_transactions.delete_many({"session_id": sid}))
        _run(db.subscriptions.delete_many({"user_id": uid}))
        _run(db.users.delete_one({"_id": ObjectId(uid)}))


# ───────────────────────────── P2-03 durable trial grant ─────────────────────────────

def test_trial_grant_is_durable_and_immune_to_later_offer_edits(pricing_snapshot):
    import plan_settings
    import subscription_service as ss
    db = _db()
    _run(plan_settings.load(db))
    _run(plan_settings.update(db, {"trial_days": 15, "trial_tier": "trader"}, {"email": "adm@example.com"}))
    created = datetime.now(timezone.utc)
    grant = ss.trial_grant_for_signup(created)
    assert grant["tier"] == "trader" and grant["days"] == 15 and grant["offer_version"] == plan_settings._state["pricing_version"]
    uid = str(ObjectId())
    _run(db.users.insert_one({"_id": ObjectId(uid), "email": f"{uid}@example.com", "role": "user",
                              "created_at": created.isoformat(), "trial_grant": grant}))
    try:
        # offer changes AFTER sign-up (different tier, shorter) — the stored grant wins
        _run(plan_settings.update(db, {"trial_days": 3, "trial_tier": "starter"}, {"email": "adm@example.com"}))
        sub = _run(ss.get_subscription(uid))
        assert sub["current_plan_id"] == "trial_trader" and sub["trial"]["days"] == 15
        assert sub["trial"]["offer_version"] == grant["offer_version"]
        assert datetime.fromisoformat(sub["valid_until"]) - created == timedelta(days=15)
        # disabling the trial entirely does not revoke an existing grant either
        _run(plan_settings.update(db, {"trial_days": 0}, {"email": "adm@example.com"}))
        _run(db.subscriptions.delete_many({"user_id": uid}))
        assert _run(ss.get_subscription(uid))["current_plan_id"] == "trial_trader"
        # expired grant yields no entitlement
        _run(db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"trial_grant.ends_at": (created - timedelta(days=1)).isoformat()}}))
        _run(db.subscriptions.delete_many({"user_id": uid}))
        assert _run(ss.get_subscription(uid))["current_plan_id"] is None
    finally:
        _run(db.subscriptions.delete_many({"user_id": uid}))
        _run(db.users.delete_one({"_id": ObjectId(uid)}))


def test_registration_route_writes_trial_grant():
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "routes", "auth_routes.py")).read()
    assert "decide_trial_at_signup" in src and 'user_doc["trial_decision"]' in src and 'user_doc["trial_grant"]' in src
