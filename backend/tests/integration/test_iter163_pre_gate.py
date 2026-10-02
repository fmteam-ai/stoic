"""iter-163 — pre-gate review corrections.

P0: canary divergence boundary = STRICTER of (+25pp, 3× fleet); matrix
    tests across fleet rates 0/5/10/20/40% and tiny samples.
P1: canary health is multidimensional (guard blocks + execution failures).
P1: performance attestation PROHIBITED on unreconciled P&L, non-FRESH
    position truth, or synthetic/test account data.
P2: legacy HMAC attestation acceptance has a hard retirement instant.
"""
import asyncio
import hashlib
import hmac
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

pytestmark = pytest.mark.integration

DB_NAME = f"stoic_test_gate_{uuid.uuid4().hex[:8]}"


def _now_dt():
    return datetime.now(timezone.utc)


def _fresh_db():
    os.environ["DB_NAME"] = DB_NAME
    import database
    database._client = None
    from database import get_db
    return get_db()


# ─────────────── P0: stricter boundary matrix ──────────────────────────────

def _v(c_rate, f_rate, n=30):
    from release_canary import divergence_verdict
    return divergence_verdict({"decisions": n, "block_rate": c_rate},
                              {"decisions": 100, "block_rate": f_rate})


def test_canary_boundary_is_stricter_of_the_two_limits():
    # fleet 0% → 3× rule gives 0% → boundary 0%: ANY canary blocking is
    # divergent once judged (a quiet fleet tolerates no canary blocks)
    assert _v(0.05, 0.0)["diverged"] is True
    assert _v(0.0, 0.0)["diverged"] is False
    # fleet 5% → min(30%, 15%) = 15%
    assert _v(0.15, 0.05)["diverged"] is False
    assert _v(0.16, 0.05)["diverged"] is True
    # fleet 10% → min(35%, 30%) = 30% (ratio rule is the stricter one)
    assert _v(0.30, 0.10)["diverged"] is False
    assert _v(0.31, 0.10)["diverged"] is True
    # fleet 20% → min(45%, 60%) = 45% (+25pp rule is the stricter one)
    assert _v(0.45, 0.20)["diverged"] is False
    assert _v(0.46, 0.20)["diverged"] is True
    # fleet 40% → min(65%, 120%→capped) = 65%
    assert _v(0.65, 0.40)["diverged"] is False
    assert _v(0.66, 0.40)["diverged"] is True


def test_canary_tiny_samples_never_judge():
    for n in (0, 1, 5, 19):
        v = _v(1.0, 0.0, n=n)
        assert v["judged"] is False and v["diverged"] is False
    assert _v(1.0, 0.0, n=20)["diverged"] is True


# ─────────────── P1: multidimensional canary health ────────────────────────

async def _multidim_scenario():
    db = _fresh_db()
    import release_canary as rc
    try:
        demo = ObjectId()
        await db.accounts.insert_one(
            {"_id": demo, "display_name": "Canary",
             "broker_environment": "DEMO"})
        await rc.enable(db, str(demo), "admin")
        now = _now_dt().isoformat()
        # guard-block dimension: healthy and judged (25 canary decisions,
        # same 20% block rate as the fleet)
        docs = [{"snapshot_id": f"c{i}", "at": now, "account_id": str(demo),
                 "authorized": i % 5 != 0} for i in range(25)]
        docs += [{"snapshot_id": f"f{i}", "at": now,
                  "account_id": "fleet-acc", "authorized": i % 5 != 0}
                 for i in range(50)]
        await db.pamm_risk_decisions.insert_many(docs)
        out = await rc.evaluate(db)
        assert out["halted"] is False
        dims = {d["dimension"]: d for d in out["dimensions"]}
        assert dims["guard_block_rate"]["judged"] is True
        assert dims["execution_failure_rate"]["judged"] is False

        # execution-health dimension diverges (6/12 UNKNOWN on canary vs
        # 0/40 on fleet) → HALT even though block rate is fine
        intents = [{"account_id": str(demo), "created_at": now,
                    "status": "unknown" if i < 6 else "filled"}
                   for i in range(12)]
        intents += [{"account_id": "fleet-acc", "created_at": now,
                     "status": "filled"} for _ in range(40)]
        await db.execution_intents.insert_many(intents)
        await db.bot_configs.insert_one(
            {"account_id": str(demo), "active": True, "user_id": "u1"})
        out = await rc.evaluate(db)
        assert out["halted"] is True
        assert "execution_failure_rate" in out["verdict"]["reason"]
        assert "guard_block_rate" not in out["verdict"]["reason"]
        st = await rc.get_state(db)
        assert st["halted"] is True
    finally:
        await db.client.drop_database(DB_NAME)


def test_canary_multidimensional_halt():
    asyncio.run(_multidim_scenario())


# ─────────────── P1: attestation prohibition gates ─────────────────────────

async def _attestation_gate_scenario():
    db = _fresh_db()
    from routes.performance_routes import _attach_attestation
    now = _now_dt()
    try:
        # clean user: ONE enabled LIVE account, verified identity, FRESH
        # heartbeat agreeing with the local projection, fresh broker deal,
        # reconciled (empty) P&L, nothing synthetic → signed
        clean_acc = ObjectId()
        await db.accounts.insert_one(
            {"_id": clean_acc, "user_id": "clean_user", "label": "Real IC",
             "mode": "live", "trading_enabled": True,
             "broker_server": "ICMarkets-Live01", "open_positions": 0,
             "last_heartbeat": (now - timedelta(seconds=5)).isoformat(),
             "last_reconciled_at": (now - timedelta(minutes=1)).isoformat(),
             "verified_identity": {"broker_server": "ICMarkets-Live01"}})
        await db.broker_deals.insert_one(
            {"user_id": "clean_user", "deal_id": 1, "account_id": "x",
             "deal_time": int((now - timedelta(minutes=5)).timestamp())})
        # round 13 P1 — no signed, RECONCILED broker statement on the ledger
        # → attestation withheld even for an otherwise clean account
        out = await _attach_attestation(db, "clean_user", {"overall": {}})
        assert out["attestation"] is None
        assert "STATEMENT_LEDGER_MISSING" in out["attestation_blocked"]["reasons"]
        # r15 P1-03 — the publication gate RE-VERIFIES: a bare RECONCILED row
        # without verifiable signature/identity is still withheld
        await db.reconciliation_ledger.insert_one(
            {"_id": f"{clean_acc}:ST-1", "user_id": "clean_user", "account_id": str(clean_acc),
             "statement_id": "ST-1", "status": "RECONCILED",
             "period_from": (now - timedelta(days=31)).isoformat(),
             "period_to": (now - timedelta(days=1)).isoformat(),
             "statement_sha256": "0" * 64})
        out = await _attach_attestation(db, "clean_user", {"overall": {}})
        assert out["attestation"] is None
        assert {"STATEMENT_IDENTITY_CHANGED", "STATEMENT_UNVERIFIABLE"} <= set(out["attestation_blocked"]["reasons"])
        # a genuinely signed, identity-bound statement (via reconcile) passes
        import broker_statement_ledger as bl
        import base64 as _b64
        from cryptography.hazmat.primitives import serialization as _ser
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        k = Ed25519PrivateKey.generate()
        pub = _b64.b64encode(k.public_key().public_bytes(_ser.Encoding.Raw, _ser.PublicFormat.Raw)).decode()
        import os as _os
        _os.environ["STATEMENT_ATTESTATION_PUBLIC_KEY_B64"] = pub
        _os.environ.pop("STATEMENT_ATTESTATION_KEYS_JSON", None)
        await db.reconciliation_ledger.delete_many({"user_id": "clean_user"})
        await db.accounts.update_one({"_id": clean_acc}, {"$set": {
            "broker": "ICMarkets", "verified_identity": {"broker_server": "ICMarkets-Live01", "account_number": "77001"},
            "broker_environment": "LIVE", "base_currency": "USD"}})
        await db.broker_deals.update_many({"user_id": "clean_user"}, {"$set": {
            "account_id": str(clean_acc), "profit": 0, "commission": 0, "swap": 0,
            "financial_reconciliation_status": "complete"}})
        st = {"schema": bl.STATEMENT_SCHEMA, "statement_id": "ST-2", "account_id": str(clean_acc), "broker_login": "77001", "currency": "USD",
              "period_from": (now - timedelta(days=31)).isoformat(), "period_to": (now - timedelta(days=1)).isoformat(),
              "issuer": "ICMarkets", "issued_at": now.isoformat(), "opening_balance": 1000, "closing_balance": 1000,
              "closing_equity": 1000, "trading_pnl": 0, "commission": 0, "swap": 0, "deposits": 0, "withdrawals": 0,
              "corrections": 0, "fx_conversion": 0}
        st = {**{k2: 0 for k2 in bl.MONEY_FIELDS}, **st}
        sig = k.sign(bl.statement_body(st)).hex()
        await bl.reconcile(db, "clean_user", st, sig, "ops@x")
        out = await _attach_attestation(db, "clean_user", {"overall": {}})
        assert out["attestation_blocked"] is None, out["attestation_blocked"]
        assert out["attestation"] is not None
        assert out["verified_ledger"]["chain_ok"] and out["verified_ledger"]["ledger_rows"] == 1

        # synthetic/test account data → PROHIBITED
        await db.accounts.insert_one(
            {"_id": ObjectId(), "user_id": "synth_user",
             "label": "test_qa_account", "trading_enabled": False})
        out = await _attach_attestation(db, "synth_user", {"overall": {}})
        assert out["attestation"] is None
        assert "SYNTHETIC_ACCOUNT_DATA" in \
            out["attestation_blocked"]["reasons"]

        # stale position truth on an ENABLED account → PROHIBITED
        await db.accounts.insert_one(
            {"_id": ObjectId(), "user_id": "stale_user", "label": "Real",
             "trading_enabled": True,
             "last_heartbeat": (now - timedelta(seconds=900)).isoformat()})
        out = await _attach_attestation(db, "stale_user", {"overall": {}})
        assert out["attestation"] is None
        assert "POSITION_TRUTH_NOT_FRESH" in \
            out["attestation_blocked"]["reasons"]
    finally:
        await db.client.drop_database(DB_NAME)


def test_attestation_prohibited_on_bad_truth_or_synthetic():
    asyncio.run(_attestation_gate_scenario())


# ─────────────── P2: legacy HMAC retirement ────────────────────────────────

def test_legacy_hmac_has_retirement_instant(monkeypatch):
    import differentiation as diff
    a = diff.perf_attestation({"x": 1})
    legacy_sig = hmac.new(
        (os.environ.get("PERF_SIGNING_KEY")
         or os.environ["JWT_SECRET"]).encode(),
        a["payload_hash"].encode(), hashlib.sha256).hexdigest()
    # before retirement → accepted
    assert diff.verify_attestation(a["payload_hash"], legacy_sig) is True
    # after retirement → REJECTED (Ed25519 still verifies)
    monkeypatch.setattr(diff, "LEGACY_HMAC_ACCEPTED_UNTIL",
                        "2020-01-01T00:00:00+00:00")
    assert diff.verify_attestation(a["payload_hash"], legacy_sig) is False
    assert diff.verify_attestation(a["payload_hash"],
                                   a["signature"]) is True
    # retirement date is a real future instant at time of writing
    monkeypatch.undo()
    assert diff.LEGACY_HMAC_ACCEPTED_UNTIL > _now_dt().isoformat()


# ─────────────── P1: shared live-target fixture ────────────────────────────

def test_live_target_resolution(monkeypatch):
    from live_target import get_base_url
    monkeypatch.setenv("REACT_APP_BACKEND_URL", "https://x.example/")
    assert get_base_url() == "https://x.example"
    monkeypatch.delenv("REACT_APP_BACKEND_URL", raising=False)
    monkeypatch.setenv("LIVE_TEST_BASE_URL", "https://y.example")
    assert get_base_url() == "https://y.example"
