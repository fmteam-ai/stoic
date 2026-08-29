"""iter-154 — STOIC Certification Center + STOIC Connect.

1. pillar_scores(): six pillars scored from real collections; missing data
   → NO_DATA (excluded from grade, never a crash).
2. grade(): tier rules (A ≥85 & ≥5 pillars; B ≥70 & ≥4; else PROVISIONAL /
   UNCERTIFIED).
3. issue_public(): hash-chained per-account certificates (seq/prev_hash),
   verify_certificate() passes pristine, fails tampered, and still passes
   after revocation (status change ≠ content change).
4. connect_status(): plain-language pipeline steps + progress.
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

DB_NAME = f"stoic_test_cert_{uuid.uuid4().hex[:8]}"


def _now_dt():
    return datetime.now(timezone.utc)


def _fresh_db():
    os.environ["DB_NAME"] = DB_NAME
    import database
    database._client = None
    from database import get_db
    return get_db()


def test_grade_tiers():
    from certification_center import grade
    mk = lambda scores: [{"pillar": f"p{i}", "score": s}
                         for i, s in enumerate(scores)]
    assert grade(mk([90, 90, 90, 90, 90, None]))["tier"] == "CERTIFIED_A"
    assert grade(mk([75, 75, 75, 75, None, None]))["tier"] == "CERTIFIED_B"
    assert grade(mk([90, 90, None, None, None, None]))["tier"] == "PROVISIONAL"
    assert grade(mk([None] * 6))["tier"] == "UNCERTIFIED"
    g = grade(mk([80, 60, None, None, None, None]))
    assert g["overall_score"] == 70.0 and g["pillars_scored"] == 2


async def _pillars_scenario():
    db = _fresh_db()
    from certification_center import pillar_scores
    acct_id = "cert_acct_1"
    account = {"_id": acct_id, "label": "Test", "broker": "ICM",
               "account_number": "1234567"}
    user_id = "cert_user_1"
    try:
        now = _now_dt().isoformat()
        # risk truth: 3 hashed of 4 decisions
        await db.pamm_risk_decisions.insert_many([
            {"account_id": acct_id, "at": now, "hash": f"h{i}",
             "authorized": True} for i in range(3)]
            + [{"account_id": acct_id, "at": now, "authorized": False}])
        # position truth: clean traced trade
        await db.trades.insert_one(
            {"account_id": acct_id, "user_id": user_id, "status": "closed",
             "opened_at": now, "signal_id": "s1", "mt5_ticket": 111,
             "latency_trace": {"t9_ms": 40}})
        # execution alpha decisions
        await db.execution_alpha_decisions.insert_one(
            {"user_id": user_id, "at": now, "action": "allow"})
        out = await pillar_scores(db, user_id, account)
        by = {p["pillar"]: p for p in out["pillars"]}
        assert by["risk_truth"]["score"] == 75.0
        assert by["risk_truth"]["metrics"]["decisions_7d"] == 4
        assert by["position_truth"]["score"] == 100.0
        assert by["execution_alpha"]["score"] == 100.0
        # twin/decay/broker have no data → NO_DATA, never crash
        assert by["digital_twin"]["status"] in ("NO_DATA", "GREEN", "YELLOW")
        assert by["strategy_decay"]["status"] == "NO_DATA"
        assert out["tier"] in ("PROVISIONAL", "CERTIFIED_B")
        assert out["pillars_total"] == 6
    finally:
        await db.client.drop_database(DB_NAME)


def test_pillar_scores_from_collections():
    asyncio.run(_pillars_scenario())


async def _cert_chain_scenario():
    db = _fresh_db()
    from certification_center import (issue_public, public_view,
                                      revoke_public, verify_certificate)
    account = {"_id": "chain_acct", "label": "Chain", "broker": "ICM",
               "account_number": "7654321"}
    user = {"id": "chain_user", "role": "user"}
    try:
        c1 = await issue_public(db, user, account)
        c2 = await issue_public(db, user, account)
        assert c1["seq"] == 1 and c1["prev_hash"] == "genesis"
        assert c2["seq"] == 2 and c2["prev_hash"] == c1["hash"]
        assert c1["account_ref"].endswith("321") and "•" in c1["account_ref"]
        assert verify_certificate(c1) and verify_certificate(c2)

        # public view strips internals
        pv = public_view(c1)
        assert "account_id" not in pv and "user_id" not in pv
        assert pv["valid"] is True

        # tamper breaks verification
        tampered = {**c1, "tier": "CERTIFIED_A", "overall_score": 99.9}
        assert verify_certificate(tampered) is False

        # revocation flips validity but the ISSUED content still verifies
        out = await revoke_public(db, c1["cert_id"], user, "test")
        assert out["ok"] is True
        stored = await db.public_certificates.find_one(
            {"cert_id": c1["cert_id"]})
        assert stored["revoked"] is True
        assert verify_certificate(stored) is True
        assert public_view(stored)["valid"] is False

        # a stranger cannot revoke
        out = await revoke_public(db, c2["cert_id"],
                                  {"id": "someone_else", "role": "user"}, "x")
        assert out["ok"] is False
    finally:
        await db.client.drop_database(DB_NAME)


def test_public_certificate_hash_chain():
    asyncio.run(_cert_chain_scenario())


async def _connect_status_scenario():
    db = _fresh_db()
    from connect_service import connect_status, install_command
    now = _now_dt().isoformat()
    account = {"_id": "conn_acct", "label": "Conn", "user_id": "u1"}
    try:
        # fresh account: only step 1 done
        s = await connect_status(db, account)
        assert s["state"] == "PENDING"
        by = {x["key"]: x for x in s["steps"]}
        assert by["account_created"]["done"] is True
        assert by["installer_paired"]["done"] is False
        assert by["ea_online"]["done"] is False
        assert s["current_step"] == "Installer paired (EA deployed)"

        # fully connected account
        account.update({"installer_paired_at": now, "last_heartbeat": now,
                        "verified_identity": {"broker_server": "ICM-Live04"}})
        await db.artifact_digests.insert_one(
            {"account_id": "conn_acct", "match": True, "artifact": "ea"})
        s = await connect_status(db, account)
        assert s["state"] == "CONNECTED"
        assert s["progress_pct"] == 100
        by = {x["key"]: x for x in s["steps"]}
        assert by["reconciliation_clean"]["done"] is True
        assert by["certified"]["done"] is False  # optional 7th step

        # certificate step flips once a valid cert exists
        await db.public_certificates.insert_one(
            {"account_id": "conn_acct", "cert_id": "STC-TEST", "seq": 1,
             "tier": "CERTIFIED_B", "revoked": False,
             "expires_at": (_now_dt() + timedelta(days=5)).isoformat()})
        s = await connect_status(db, account)
        assert s["certificate_id"] == "STC-TEST"
        assert {x["key"]: x for x in s["steps"]}["certified"]["done"] is True

        cmd = install_command("https://x.example", "tok123")
        assert "installer.ps1" in cmd and 'Install-Stoic -Token "tok123"' in cmd
    finally:
        await db.client.drop_database(DB_NAME)


def test_connect_status_pipeline():
    asyncio.run(_connect_status_scenario())
