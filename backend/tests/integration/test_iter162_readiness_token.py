"""iter-162 — audit F-01/F-02/F-03/F-05 corrections.

Trading Readiness: one canonical policy state (READY/DEGRADED/CLOSE_ONLY/
BLOCKED/EMERGENCY) with stable reason codes, first-seen persistence and
recovery actions — independent of infrastructure uptime.
Bridge token: masked status endpoint (full secret shown once at rotation),
revoke endpoint, last-used tracking.
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

pytestmark = pytest.mark.integration

DB_NAME = f"stoic_test_ready_{uuid.uuid4().hex[:8]}"


def _now_dt():
    return datetime.now(timezone.utc)


def _fresh_db():
    os.environ["DB_NAME"] = DB_NAME
    import database
    database._client = None
    from database import get_db
    return get_db()


async def _readiness_scenario():
    db = _fresh_db()
    from trading_readiness import readiness
    uid = "ready_user"
    now = _now_dt()
    try:
        # no accounts → DEGRADED with NO_ENABLED_ACCOUNTS
        out = await readiness(db, uid)
        assert out["level"] == "DEGRADED"
        assert out["reasons"][0]["code"] == "NO_ENABLED_ACCOUNTS"

        # enabled account + bot with FRESH heartbeat → READY
        acc = ObjectId()
        await db.accounts.insert_one(
            {"_id": acc, "user_id": uid, "label": "main",
             "trading_enabled": True, "open_positions": 0,
             "verified_identity": True, "ea_version": "1.57",   # r18/r20: capable EA + binary proof
             "ea_binary_sha256": "c" * 64,
             "ea_binary_sha256_method": "installer_attested",   # r25 P1-01: installer-measured proof
             "last_heartbeat": (now - timedelta(seconds=5)).isoformat()})
        await db.bot_configs.insert_one(
            {"user_id": uid, "account_id": str(acc), "active": True})
        out = await readiness(db, uid)
        assert out["level"] == "READY" and out["reasons"] == []

        # stale heartbeat → position truth STALE → BLOCKED, even though
        # infrastructure would still be 'operational' (F-02 separation)
        await db.accounts.update_one(
            {"_id": acc},
            {"$set": {"last_heartbeat":
                      (now - timedelta(seconds=900)).isoformat()}})
        out = await readiness(db, uid)
        assert out["level"] == "BLOCKED"
        codes = {r["code"] for r in out["reasons"]}
        assert "POSITION_TRUTH_STALE" in codes
        r = next(x for x in out["reasons"]
                 if x["code"] == "POSITION_TRUTH_STALE")
        assert r["accounts"][0]["label"] == "main"
        assert "reconcil" in r["recovery"].lower()
        first_seen = r["first_seen"]
        assert first_seen

        # first_seen is PERSISTED across checks while the condition holds
        out2 = await readiness(db, uid)
        r2 = next(x for x in out2["reasons"]
                  if x["code"] == "POSITION_TRUTH_STALE")
        assert r2["first_seen"] == first_seen

        # recovery clears the code AND its first_seen
        await db.accounts.update_one(
            {"_id": acc},
            {"$set": {"last_heartbeat": _now_dt().isoformat()}})
        out3 = await readiness(db, uid)
        # r20 P2-04: the canonical fingerprint now sees the heartbeat recovery
        # immediately, so the stability window (RECOVERY_WINDOW, CLOSE_ONLY) applies
        # instead of a stale READY snapshot; POSITION_TRUTH_STALE itself is cleared.
        codes3 = [r["code"] for r in out3["reasons"]]
        assert "POSITION_TRUTH_STALE" not in codes3
        assert out3["level"] == "READY" or codes3 == ["RECOVERY_WINDOW"], out3["reasons"]
        doc = await db.trading_readiness.find_one({"_id": uid})
        assert "POSITION_TRUTH_STALE" not in (doc.get("first_seen") or {})

        # unknown execution on the user's account → BLOCKED
        await db.execution_intents.insert_one(
            {"account_id": str(acc), "status": "unknown"})
        out4 = await readiness(db, uid)
        assert out4["level"] == "BLOCKED"
        assert any(r["code"] == "RECONCILIATION_PENDING"
                   for r in out4["reasons"])
    finally:
        await db.client.drop_database(DB_NAME)


def test_trading_readiness_levels(monkeypatch):
    monkeypatch.setenv("EA_RELEASE_SHA256", "c" * 64)   # r20 P1-01: pinned release hash for the live gate
    asyncio.run(_readiness_scenario())


def test_readiness_level_ordering():
    from trading_readiness import LEVELS, worse
    assert worse("READY", "BLOCKED") == "BLOCKED"
    assert worse("EMERGENCY", "CLOSE_ONLY") == "EMERGENCY"
    assert LEVELS == ("READY", "DEGRADED", "CLOSE_ONLY", "BLOCKED",
                      "EMERGENCY")


async def _bridge_token_scenario():
    db = _fresh_db()
    from routes.account_routes import get_bridge_token, revoke_bridge_token
    uid = "tok_user"
    acc = ObjectId()
    try:
        await db.accounts.insert_one(
            {"_id": acc, "user_id": uid, "label": "tok",
             "bridge_token": "secret_token_abcd1234",
             "bridge_last_used_at": "2026-08-30T10:00:00+00:00"})
        out = await get_bridge_token(str(acc), user={"id": uid})
        # the FULL secret is never returned from the status endpoint
        assert "bridge_token" not in out
        assert out["bridge_token_masked"].endswith("1234")
        assert "secret_token" not in out["bridge_token_masked"]
        assert out["has_bridge_token"] is True
        assert out["last_used_at"] == "2026-08-30T10:00:00+00:00"

        # revoke kills EA auth immediately and is audited (P1-01: step-up protected — bypass header)
        from unittest.mock import patch as _patch

        async def _no_step_up(*a, **k):
            return None
        with _patch("step_up.require_step_up", _no_step_up):
            res = await revoke_bridge_token(str(acc), request=None, user={"id": uid})
        assert res["revoked"] is True
        doc = await db.accounts.find_one({"_id": acc})
        assert "bridge_token" not in doc and "bridge_token_hash" not in doc
        assert doc.get("bridge_token_revoked_at")
        audit = await db.audit_log.find_one(
            {"action": "bridge_token_revoked"})
        assert audit and audit["actor"] == uid

        out = await get_bridge_token(str(acc), user={"id": uid})
        assert out["has_bridge_token"] is False
    finally:
        await db.client.drop_database(DB_NAME)


def test_bridge_token_masking_and_revoke():
    asyncio.run(_bridge_token_scenario())
