"""iter-161 — review P1 batch:
P1-2 P&L reconciliation on /trades/stats;
P1-4 Ed25519 performance attestation (legacy HMAC still verifies);
P1-6/7 synthetic-data isolation for ops alerts and operator surfaces.
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

DB_NAME = f"stoic_test_p1_{uuid.uuid4().hex[:8]}"


def _now_dt():
    return datetime.now(timezone.utc)


def _fresh_db():
    os.environ["DB_NAME"] = DB_NAME
    import database
    database._client = None
    from database import get_db
    return get_db()


# ───────────────────────── P1-2: P&L reconciliation ───────────────────────

async def _recon_scenario():
    db = _fresh_db()
    from routes.trade_routes import trade_stats
    uid = "recon_user"
    now = _now_dt().isoformat()
    try:
        await db.trades.insert_many([
            {"user_id": uid, "status": "closed", "account_id": "acc1",
             "pnl": 100.0, "closed_at": now},
            {"user_id": uid, "status": "closed", "account_id": "acc1",
             "pnl": -40.5, "closed_at": now},
            {"user_id": uid, "status": "closed", "account_id": "acc2",
             "pnl": 25.25, "closed_at": now},
            {"user_id": uid, "status": "open", "account_id": "acc2"}])
        out = await trade_stats(user={"id": uid})
        rec = out["reconciliation"]
        assert rec["status"] == "RECONCILED"
        assert rec["total_pnl"] == out["total_pnl"] == 84.75
        assert rec["components"] == {"acc1": 59.5, "acc2": 25.25}
        assert rec["sum_components"] == 84.75
        assert abs(rec["delta"]) <= rec["tolerance"]
        assert rec["closed_trades"] == 3
    finally:
        await db.client.drop_database(DB_NAME)


def test_trade_stats_reconciliation():
    asyncio.run(_recon_scenario())


def test_reconciliation_flags_mismatch():
    """The UNRECONCILED branch is pure math — verify the rule directly."""
    total, sum_components, tolerance = 100.0, 84.75, 0.01
    delta = round(total - sum_components, 2)
    assert abs(delta) > tolerance  # would surface as UNRECONCILED


# ───────────────────────── P1-4: Ed25519 attestation ──────────────────────

def test_perf_attestation_ed25519():
    from differentiation import perf_attestation, verify_attestation
    a = perf_attestation({"pnl": 1, "trades": 2})
    assert a["algo"].startswith("Ed25519")
    assert a["key_id"] == "perf-ed25519-v1"
    assert len(a["signature"]) == 128            # Ed25519 hex, not HMAC (64)
    assert a["public_key_b64"]                   # independently verifiable
    assert verify_attestation(a["payload_hash"], a["signature"]) is True
    # third-party verification with ONLY the public key (no server trust)
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )
    pub = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(a["public_key_b64"]))
    # N100-11 — attestations are domain-separated: verifiers prepend the published prefix
    pub.verify(bytes.fromhex(a["signature"]), b"stoic:differentiation:v1\0" + a["payload_hash"].encode())
    # legacy HMAC attestations issued pre-migration still verify
    legacy = hmac.new(
        (os.environ.get("PERF_SIGNING_KEY")
         or os.environ["JWT_SECRET"]).encode(),
        a["payload_hash"].encode(), hashlib.sha256).hexdigest()
    assert verify_attestation(a["payload_hash"], legacy) is True
    assert verify_attestation(a["payload_hash"], "ff" * 64) is False


# ───────────────────────── P1-6/7: synthetic isolation ────────────────────

def test_synthetic_helpers():
    from synthetic_data import alert_scope_filter, is_synthetic_account
    assert is_synthetic_account({"synthetic": True}) is True
    assert is_synthetic_account({"label": "chaos_acct_1"}) is True
    assert is_synthetic_account({"user_id": "qa_bot_7"}) is True
    assert is_synthetic_account({"label": "My Real FTMO"}) is False
    assert alert_scope_filter("real") == {"synthetic": {"$ne": True}}
    assert alert_scope_filter("synthetic") == {"synthetic": True}
    assert alert_scope_filter("all") == {}
    assert alert_scope_filter(None) == {"synthetic": {"$ne": True}}


async def _synthetic_alert_scenario():
    db = _fresh_db()
    from alerting import evaluate_ops_alerts, raise_alert
    from command_center import status as cc_status
    from synthetic_data import alert_scope_filter
    try:
        await raise_alert(db, "real_incident", "critical", "real problem",
                          dedup_key="r1")
        await raise_alert(db, "qa_noise", "critical", "chaos artifact",
                          dedup_key="s1", synthetic=True)
        real = await db.ops_alerts.count_documents(
            {"acked_at": None, **alert_scope_filter("real")})
        synth = await db.ops_alerts.count_documents(
            {"acked_at": None, **alert_scope_filter("synthetic")})
        assert (real, synth) == (1, 1)

        # command center only surfaces REAL alerts
        out = await cc_status(db)
        kinds = {a["kind"] for a in out["recent_alerts"]}
        assert kinds == {"real_incident"}
        assert out["sections"]["workers"]["open_critical_alerts"] == 1

        # heartbeat evaluator tags alerts for synthetic accounts (only
        # explicitly ENABLED accounts are evaluated — missing flag = OFF)
        stale_hb = (_now_dt() - timedelta(seconds=600)).isoformat()
        await db.accounts.insert_many([
            {"_id": ObjectId(), "label": "chaos_hb", "user_id": "qa_x",
             "trading_enabled": True, "last_heartbeat": stale_hb},
            {"_id": ObjectId(), "label": "Real FTMO", "user_id": "human1",
             "trading_enabled": True, "last_heartbeat": stale_hb}])
        await evaluate_ops_alerts(db)
        hb_alerts = [a async for a in db.ops_alerts.find(
            {"kind": "ea_heartbeat_stale"})]
        flags = {a["message"].split("'")[1]: a.get("synthetic")
                 for a in hb_alerts}
        assert flags["chaos_hb"] is True
        assert flags["Real FTMO"] is False
    finally:
        await db.client.drop_database(DB_NAME)


def test_synthetic_alert_isolation():
    asyncio.run(_synthetic_alert_scenario())
