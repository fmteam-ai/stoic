"""Audit r29 — P1-01 immutable checkout snapshot · P1-02 DEMO proof + audited
override · P2-01 watermark provenance · P2-02 trial decision · P2-03 snapshot
proration · P2-04 vault key rotation · P2-05 typed provenance."""
import asyncio
import base64
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

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


ADMIN = {"id": str(ObjectId()), "email": "adm29@example.com", "role": "admin", "two_factor_enabled": True}


def _acc(uid, **over):
    base = {"user_id": uid, "label": "r29", "mode": "live", "account_type": "demo", "broker": "VT Markets",
            "server": "VTMarkets-Demo", "account_number": "1289887", "bridge_token": f"r29-{uid}-{over.get('n', 0)}",
            "ea_version": "1.58", "broker_account_id_reported": "1289887",
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "ea_identity": {"installation_id": "inst-A", "authoritative": True, "broker_server": "VTMarkets-Demo"}}
    over.pop("n", None)
    base.update(over)
    return base


# ───────────────────────── P1-02 DEMO truth ─────────────────────────

def test_demo_proof_checks():
    from broker_env import demo_proof
    ok = demo_proof(_acc("u"))
    assert ok["ok"] and ok["mandatory_ok"] and all(ok["checks"].values())
    stale = demo_proof(_acc("u", last_heartbeat=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()))
    assert not stale["mandatory_ok"] and stale["checks"]["heartbeat_fresh"] is False
    unauth = demo_proof(_acc("u", ea_identity={"installation_id": "x", "authoritative": False, "broker_server": "VTMarkets-Demo"}))
    assert unauth["checks"]["terminal_identity_authoritative"] is False
    wrong_login = demo_proof(_acc("u", broker_account_id_reported="999"))
    assert wrong_login["checks"]["login_reported_matches_account"] is False
    ecn = demo_proof(_acc("u", server="RoboForex-ECN", ea_identity={"installation_id": "i", "authoritative": True, "broker_server": "RoboForex-ECN"}))
    assert ecn["mandatory_ok"] and not ecn["ok"] and ecn["override_eligible"]
    live_flag = demo_proof(_acc("u", broker_account_mismatch=True))
    assert live_flag["checks"]["no_live_capital_indicator"] is False


def test_attestation_requires_proof_and_override_is_audited(monkeypatch):
    import routes.admin_routes as ar
    from broker_env import attested_environment
    db = _db()
    uid = str(ObjectId())

    async def _ok(*a, **k):
        return None
    monkeypatch.setattr(ar, "_reauth", _ok)
    stale_id = _run(db.accounts.insert_one(_acc(uid, n=1, last_heartbeat=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat()))).inserted_id
    ecn_id = _run(db.accounts.insert_one(_acc(uid, n=2, server="RoboForex-ECN",
                                              ea_identity={"installation_id": "i2", "authoritative": True, "broker_server": "RoboForex-ECN"}))).inserted_id
    good_id = _run(db.accounts.insert_one(_acc(uid, n=3))).inserted_id
    try:
        with pytest.raises(HTTPException) as ei:
            _run(ar.admin_attest_account_environment(str(stale_id), {"environment": "DEMO", "password": "x"}, ADMIN))
        assert ei.value.status_code == 409 and ei.value.detail["code"] == "demo_proof_missing"
        assert "heartbeat_fresh" in ei.value.detail["failed_checks"]
        # non-demo-named server: refused without override, refused with override but no reason
        with pytest.raises(HTTPException) as ei:
            _run(ar.admin_attest_account_environment(str(ecn_id), {"environment": "DEMO", "password": "x"}, ADMIN))
        assert ei.value.detail["code"] == "server_not_demo_named"
        with pytest.raises(HTTPException) as ei:
            _run(ar.admin_attest_account_environment(str(ecn_id), {"environment": "DEMO", "password": "x", "override": True, "reason": "ok"}, ADMIN))
        assert ei.value.status_code == 422
        row = _run(ar.admin_attest_account_environment(str(ecn_id), {"environment": "DEMO", "password": "x", "override": True,
                                                                     "reason": "practice account confirmed with broker"}, ADMIN))
        assert row["effective"] == "DEMO" and row["verifier"] == "admin_override"
        audit = _run(db.admin_audit_log.find_one({"target_id": str(ecn_id), "action": "account_environment_attest"}))
        assert audit["meta"]["override"] is True and audit["meta"]["verifier"] == "admin_override" and audit["meta"]["proof_id"]
        # fully proven demo: no override needed, verifier = ea_heartbeat, proof recorded on the account
        row = _run(ar.admin_attest_account_environment(str(good_id), {"environment": "DEMO", "password": "x"}, ADMIN))
        assert row["verifier"] == "ea_heartbeat"
        acc = _run(db.accounts.find_one({"_id": good_id}))
        assert acc["environment_attestation"]["proof"]["checks"]["server_demo_named"] is True
        assert attested_environment(acc) == "DEMO"
        # legacy attestation (no proof) is not honoured
        _run(db.accounts.update_one({"_id": good_id}, {"$unset": {"environment_attestation.proof": ""}}))
        assert attested_environment(_run(db.accounts.find_one({"_id": good_id}))) == "LIVE"
    finally:
        _run(db.accounts.delete_many({"user_id": uid}))
        _run(db.admin_audit_log.delete_many({"target_id": {"$in": [str(stale_id), str(ecn_id), str(good_id)]}}))


# ───────────────────────── P1-01 / P2-03 immutable checkout snapshot ─────────────────────────

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


def test_checkout_snapshot_survives_concurrent_admin_pricing_update(pricing_snapshot, monkeypatch):
    """Admin changes prices DURING the Stripe await: ledger, metadata, response and
    fulfilment must all reflect the single snapshot taken before the await."""
    import plan_settings
    import subscription_plans as sp
    import routes.subscription_routes as sr
    db = _db()
    _run(plan_settings.load(db))
    _run(plan_settings.update(db, {"base_cents": {"trader": 9900}, "currency": "usd"}, {"email": "a@x"}))
    v0 = sp.PRICING_VERSION
    captured = {}

    class _Stripe:
        async def create_checkout_session(self, req):
            captured["req"] = req
            # admin edits pricing while Stripe is being called
            await plan_settings.update(db, {"base_cents": {"trader": 14900}, "currency": "eur"}, {"email": "a@x"})
            class S:
                session_id = f"cs_r29_{ObjectId()}"
                url = "https://stripe.test/x"
            return S()

    monkeypatch.setattr(sr, "_stripe_client", lambda host: _Stripe())
    monkeypatch.setenv("CHECKOUT_ALLOWED_ORIGINS", "https://app.test")
    uid = str(ObjectId())
    user = {"id": uid, "email": f"{uid}@x", "role": "user"}

    class R:
        base_url = "https://api.test/"
    out = _run(sr.create_checkout({"plan_id": "trader_monthly", "origin": "https://app.test"}, R(), user))
    try:
        assert sp.PRICING_VERSION == v0 + 1 and sp.CURRENCY == "eur"           # globals DID move
        assert captured["req"].amount == 99.0 and captured["req"].currency == "usd"
        assert captured["req"].metadata["amount_minor"] == "9900" and captured["req"].metadata["pricing_version"] == str(v0)
        assert out["plan"]["amount_minor"] == 9900 and out["plan"]["currency"] == "usd" and out["pricing_version"] == v0
        txn = _run(db.payment_transactions.find_one({"session_id": out["session_id"]}))
        assert txn["amount_minor"] == 9900 and txn["currency"] == "usd" and txn["pricing_version"] == v0
        assert txn["snapshot"]["tier_base_cents"]["trader"] == 9900 and txn["idempotency_key"] == out["idempotency_key"]
        assert "public" not in txn["snapshot"]
        # fulfilment at the OLD settled amount is accepted (no false mismatch), credited under snapshot pricing
        import subscription_service as ss
        _run(db.users.insert_one({"_id": ObjectId(uid), "email": user["email"], "role": "user",
                                  "created_at": datetime.now(timezone.utc).isoformat(), "trial_decision": {"status": "not_eligible"}}))
        sub = _run(ss.apply_successful_payment(out["session_id"], source="poll", paid_amount_minor=9900, paid_currency="usd"))
        assert sub and sub["current_plan_id"] == "trader_monthly"
        assert _run(db.subscriptions.find_one({"user_id": uid}))["fulfilled_pricing_version"] == v0
    finally:
        _run(db.payment_transactions.delete_many({"user_id": uid}))
        _run(db.subscriptions.delete_many({"user_id": uid}))
        _run(db.users.delete_many({"_id": ObjectId(uid)}))


def test_upgrade_proration_uses_snapshot_base_prices(pricing_snapshot):
    import subscription_service as ss
    import subscription_plans as sp
    db = _db()
    uid, sid = str(ObjectId()), f"cs_r29p_{ObjectId()}"
    now = datetime.now(timezone.utc)
    _run(db.users.insert_one({"_id": ObjectId(uid), "email": f"{uid}@x", "role": "user",
                              "created_at": now.isoformat(), "trial_decision": {"status": "not_eligible"}}))
    _run(db.subscriptions.insert_one({"user_id": uid, "current_plan_id": "starter_monthly",
                                      "valid_until": (now + timedelta(days=30)).isoformat()}))
    try:
        snap_bases = {"starter": 1000, "trader": 2000, "professional": 40000, "elite": 80000}   # 2:1 at checkout time
        _run(ss.record_transaction(user_id=uid, user_email="x", plan_id="trader_monthly", session_id=sid, amount_usd=20.0,
                                   metadata={}, amount_cents=2000, currency="usd", pricing_version=3,
                                   snapshot={"plan_id": "trader_monthly", "tier": "trader", "duration_months": 1,
                                             "tier_base_cents": snap_bases, "pricing_version": 3}))
        # current catalog is wildly different — must not influence the credit
        sp.apply_pricing({"starter": 100, "trader": 100000}, {}, "usd", 99)
        sub = _run(ss.apply_successful_payment(sid, source="webhook", paid_amount_minor=2000, paid_currency="usd"))
        credited = sub["proration"]["credited_days"]
        assert 14.5 <= credited <= 15.1, credited      # 30 remaining days × (1000/2000) from the SNAPSHOT
    finally:
        _run(db.payment_transactions.delete_many({"session_id": sid}))
        _run(db.subscriptions.delete_many({"user_id": uid}))
        _run(db.users.delete_many({"_id": ObjectId(uid)}))


# ───────────────────────── P2-02 trial decision ─────────────────────────

def test_trial_decision_pending_error_is_retried_against_original_offer(pricing_snapshot, monkeypatch):
    import plan_settings
    import subscription_service as ss
    db = _db()
    _run(plan_settings.load(db))
    _run(plan_settings.update(db, {"trial_days": 15, "trial_tier": "trader"}, {"email": "a@x"}))
    v = plan_settings._state["pricing_version"]
    created = datetime.now(timezone.utc)

    async def _boom(_db):
        raise RuntimeError("mongo hiccup")
    monkeypatch.setattr(plan_settings, "ensure_fresh", _boom)
    dec = _run(ss.decide_trial_at_signup(db, created))
    assert dec["status"] == "pending_error" and dec["attempts"] == 1 and "created_at" in dec
    monkeypatch.undo()
    uid = str(ObjectId())
    _run(db.users.insert_one({"_id": ObjectId(uid), "email": f"{uid}@x", "role": "user",
                              "created_at": created.isoformat(), "trial_decision": {**dec, "offer_version": v}}))
    try:
        # offer moved after sign-up → retry must NOT grant the newer offer
        _run(plan_settings.update(db, {"trial_days": 3, "trial_tier": "starter"}, {"email": "a@x"}))
        sub = _run(ss.get_subscription(uid))
        u = _run(db.users.find_one({"_id": ObjectId(uid)}))
        assert u["trial_decision"]["status"] == "pending_error" and "offer moved" in u["trial_decision"]["note"]
        assert sub["current_plan_id"] is None            # never lazily evaluated against the newer offer
        # offer restored to the pinned version → idempotent retry grants the ORIGINAL 15-day trader trial
        _run(db.platform_state.update_one({"_id": plan_settings.DOC_ID},
                                          {"$set": {"trial_days": 15, "trial_tier": "trader", "pricing_version": v}}))
        _run(db.subscriptions.delete_many({"user_id": uid}))
        sub = _run(ss.get_subscription(uid))
        u = _run(db.users.find_one({"_id": ObjectId(uid)}))
        assert u["trial_decision"]["status"] == "granted" and u["trial_decision"]["retried_from"] == "pending_error"
        assert sub["current_plan_id"] == "trial_trader" and sub["trial"]["days"] == 15
        # explicit not_eligible is final — no lazy fallback
        _run(db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"trial_decision": {"status": "not_eligible"}}, "$unset": {"trial_grant": ""}}))
        _run(db.subscriptions.delete_many({"user_id": uid}))
        assert _run(ss.get_subscription(uid))["current_plan_id"] is None
    finally:
        _run(db.subscriptions.delete_many({"user_id": uid}))
        _run(db.users.delete_many({"_id": ObjectId(uid)}))


# ───────────────────────── P2-01 watermark provenance ─────────────────────────

def test_verified_performance_as_of_is_authoritative_watermark_not_now():
    from routes.performance_routes import _verified_payload
    db = _db()
    uid = str(ObjectId())
    old = datetime.now(timezone.utc) - timedelta(days=3)
    acc_id = _run(db.accounts.insert_one({"user_id": uid, "label": "wm", "mode": "live", "trading_enabled": True,
                                          "bridge_token": f"wm-{uid}", "last_heartbeat": old.isoformat(),
                                          "last_reconciled_at": old.isoformat(), "reconciliation_seq": 7})).inserted_id
    try:
        p = _run(_verified_payload(db, uid))["provenance"]
        assert p["as_of"] is not None and p["freshness_s"] >= 3 * 86400 - 60
        assert p["stale"] is True and p["share_allowed"] is False
        assert p["reconciliation_id"].startswith("recon:") and ":7" in p["reconciliation_id"]
        assert "hb_age" not in str(p["reconciliation_id"])
        # fresh reconciliation + heartbeat → not stale, share allowed
        now = datetime.now(timezone.utc).isoformat()
        _run(db.accounts.update_one({"_id": acc_id}, {"$set": {"last_heartbeat": now, "last_reconciled_at": now}}))
        p = _run(_verified_payload(db, uid))["provenance"]
        assert p["stale"] is False and p["share_allowed"] is True
        # no reconciliation at all → stale regardless of generation time
        _run(db.accounts.update_one({"_id": acc_id}, {"$unset": {"last_reconciled_at": ""}}))
        p = _run(_verified_payload(db, uid))["provenance"]
        assert p["stale"] is True and p["cache_status"] == "unreconciled"
    finally:
        _run(db.accounts.delete_many({"user_id": uid}))


# ───────────────────────── P2-04 vault key rotation ─────────────────────────

def test_vault_dual_read_and_rewrap_manifest(monkeypatch):
    import integrations_settings as integ
    db = _db()
    k_old = base64.b64encode(b"o" * 32).decode()
    k_new = base64.b64encode(b"n" * 32).decode()
    key = "RESEND_API_KEY"
    saved = _run(db.secrets_vault.find_one({"_id": key}))
    try:
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("SECRETS_MASTER_KEY", k_old)
        monkeypatch.delenv("SECRETS_MASTER_KEY_PREVIOUS", raising=False)
        monkeypatch.setenv("SECRETS_MASTER_KEY_VERSION", "1")
        sealed_old = integ.seal("re_dummy_value_1234")
        assert sealed_old["key_version"] == 1
        _run(db.secrets_vault.replace_one({"_id": key}, {"_id": key, **sealed_old}, upsert=True))
        # rotate: new current key, old as previous → dual-read works, single-write uses new
        monkeypatch.setenv("SECRETS_MASTER_KEY", k_new)
        monkeypatch.setenv("SECRETS_MASTER_KEY_PREVIOUS", k_old)
        monkeypatch.setenv("SECRETS_MASTER_KEY_VERSION", "2")
        assert integ.unseal(sealed_old) == "re_dummy_value_1234"
        assert integ.seal("x")["key_version"] == 2
        m = _run(integ.rewrap_all(db, {"email": "adm@x"}))
        assert m["status"] == "complete" and key in m["rewrapped"] and m["to_key_version"] == 2
        doc = _run(db.secrets_vault.find_one({"_id": key}))
        assert doc["key_version"] == 2 and doc["master_key_id"] == integ.master_key_id()
        # old key removed → still opens with the current key alone
        monkeypatch.delenv("SECRETS_MASTER_KEY_PREVIOUS")
        assert integ.unseal(doc) == "re_dummy_value_1234"
        rr = _run(integ.rewrap_readiness(db))
        assert rr["ok"] is True
        # production with the previous key still configured is a readiness blocker
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("SECRETS_MASTER_KEY_PREVIOUS", k_old)
        assert integ.readiness_check()["ok"] is False
    finally:
        if saved:
            _run(db.secrets_vault.replace_one({"_id": key}, saved, upsert=True))
        else:
            _run(db.secrets_vault.delete_one({"_id": key}))
        _run(db.secrets_rewrap_manifests.delete_many({"actor": "adm@x"}))


# ───────────────────────── P2-05 typed provenance ─────────────────────────

def test_provenance_is_typed_and_rejects_invalid_shapes():
    import chart_provenance as cp
    from pydantic import ValidationError
    p = cp.build(provider="x", source_kind="derived", points=[])
    cp.ChartProvenance.model_validate(p)
    with pytest.raises(ValidationError):
        cp.ChartProvenance.model_validate({**p, "source_kind": "guess"})
    with pytest.raises(ValidationError):
        cp.ChartProvenance.model_validate({k: v for k, v in p.items() if k != "as_of"})
    assert set(cp.FINANCIAL_SERIES_ENDPOINTS) >= {"GET /api/market/history/{symbol}", "GET /api/performance/verified"}
    ui = open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "src", "components", "ChartProvenance.jsx")).read()
    assert "PROVENANCE MISSING" in ui
