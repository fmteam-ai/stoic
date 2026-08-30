"""iter-152 — Command Center + Evidence Export + Guard Email Alerts.

1. status(): GREEN/YELLOW/RED aggregation over soak, certifications,
   guard health and workers, with worst-of overall.
2. evidence_report(): every section hash-chains via
   sha256(prev_hash + canonical json); verify_report() passes on the
   pristine report and FAILS on any tampering.
3. guard_alerts cooldown: exactly one email claim per key per window;
   unconfigured Resend records last_error instead of sending.
4. Guard block snapshots trigger the alert hook path.
"""
import asyncio
import copy
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

DB_NAME = f"stoic_test_cc_{uuid.uuid4().hex[:8]}"


def _now_dt():
    return datetime.now(timezone.utc)


def _fresh_db():
    os.environ["DB_NAME"] = DB_NAME
    import database
    database._client = None
    from database import get_db
    return get_db()


async def _status_scenario():
    db = _fresh_db()
    from command_center import status, worst
    try:
        # empty platform → soak YELLOW (no campaign), certs YELLOW,
        # guard GREEN, workers GREEN ⇒ overall YELLOW
        out = await status(db)
        assert out["overall"] == "YELLOW"
        assert out["sections"]["soak"]["status"] == "YELLOW"
        assert out["sections"]["certifications"]["status"] == "YELLOW"
        assert out["sections"]["guard"]["status"] == "GREEN"
        assert "email_alerts_configured" in out
        assert out["provenance"]["guard_policy_version"]

        # a valid certification turns certs GREEN
        await db.certifications.insert_one({
            "cert_id": "cert_cc1", "kind": "system", "subject": "acct",
            "passed": True, "checks": [], "user_id": "u1",
            "issued_at": _now_dt().isoformat(),
            "expires_at": (_now_dt() + timedelta(days=7)).isoformat(),
            "revoked": False})
        out = await status(db)
        assert out["sections"]["certifications"]["status"] == "GREEN"

        # a stale-telemetry alert forces guard RED and overall RED
        await db.ops_alerts.insert_one({
            "kind": "ea_heartbeat_stale", "severity": "critical",
            "message": "stale", "dedup_key": "ea_heartbeat:x",
            "created_at": _now_dt().isoformat(), "acked_at": None,
            "occurrences": 1})
        out = await status(db)
        assert out["sections"]["guard"]["status"] == "RED"
        assert out["overall"] == "RED"

        # blocked decision in the last hour (non-unknown) after acking the
        # stale alert → guard YELLOW
        await db.ops_alerts.update_many(
            {}, {"$set": {"acked_at": _now_dt().isoformat()}})
        await db.pamm_risk_decisions.insert_one({
            "snapshot_id": "rds_cc1", "at": _now_dt().isoformat(),
            "authorized": False, "reason": "spread_too_wide",
            "program_id": "p1", "mode": "LIVE"})
        out = await status(db)
        g = out["sections"]["guard"]
        assert g["status"] == "YELLOW"
        assert g["blocked_1h"] == 1
        assert g["top_block_reasons"][0]["reason"] == "spread_too_wide"

        # worst-of is total order
        assert worst(["GREEN", "YELLOW", "RED"]) == "RED"
        assert worst(["GREEN", "GREEN"]) == "GREEN"
        assert worst([]) == "YELLOW"
    finally:
        await db.client.drop_database(DB_NAME)


def test_command_center_status_aggregation():
    asyncio.run(_status_scenario())


async def _evidence_scenario():
    db = _fresh_db()
    from command_center import evidence_report, verify_report
    try:
        await db.pamm_risk_decisions.insert_one({
            "snapshot_id": "rds_ev1", "at": _now_dt().isoformat(),
            "authorized": False, "reason": "risk_unknown",
            "program_id": "p1", "mode": "LIVE", "hash": "h1",
            "signal": {"symbol": "XAUUSD", "side": "BUY"}})
        report = await evidence_report(db, "tester@stoic")
        assert report["report"] == "stoic-production-evidence"
        names = [s["section"] for s in report["sections"]]
        assert names == ["provenance", "certifications", "soak_status",
                         "soak_evidence_chain", "guard_health",
                         "release_canary"]
        # chain integrity holds on the pristine report
        assert verify_report(report) is True
        # guard section carries the block snapshot reference
        gh = report["sections"][-2]["data"]
        assert gh["recent_block_snapshots"][0]["snapshot_id"] == "rds_ev1"

        # ANY tamper breaks verification — data, order, or hash
        tampered = copy.deepcopy(report)
        tampered["sections"][1]["data"]["count"] = 999
        assert verify_report(tampered) is False
        reordered = copy.deepcopy(report)
        reordered["sections"].reverse()
        assert verify_report(reordered) is False
        forged = copy.deepcopy(report)
        forged["report_hash"] = "0" * 64
        assert verify_report(forged) is False
    finally:
        await db.client.drop_database(DB_NAME)


def test_evidence_report_hash_chain():
    asyncio.run(_evidence_scenario())


async def _alert_scenario():
    db = _fresh_db()
    import guard_alerts
    from guard_alerts import _claim_cooldown, email_admins
    try:
        # cooldown: exactly one claim per window, reopens after expiry
        assert await _claim_cooldown(db, "k1", 3600) is True
        assert await _claim_cooldown(db, "k1", 3600) is False
        await db.guard_alert_emails.update_one(
            {"_id": "k1"},
            {"$set": {"last_sent_at":
                      (_now_dt() - timedelta(hours=2)).isoformat()}})
        assert await _claim_cooldown(db, "k1", 3600) is True

        # unconfigured Resend → recorded, not sent (preview may have a key,
        # so force-unset for this assertion)
        import email_sender
        orig = email_sender._API_KEY
        email_sender._API_KEY = ""
        try:
            out = await email_admins(db, "s", "<b>x</b>", "k2")
            assert out == {"ok": False, "skipped": "not_configured"}
            doc = await db.guard_alert_emails.find_one({"_id": "k2"})
            assert doc["last_error"] == "email_not_configured"
        finally:
            email_sender._API_KEY = orig

        # block-alert task path runs end-to-end without raising
        snap = {"snapshot_id": "rds_a1", "reason": "risk_unknown",
                "program_id": "p1", "account_id": "a1", "mode": "LIVE",
                "at": _now_dt().isoformat(),
                "signal": {"symbol": "XAUUSD", "side": "BUY"},
                "provenance": {"git_commit": "c" * 40}}
        guard_alerts.queue_block_alert(db, snap)
        await asyncio.sleep(0.2)
        rec = await db.guard_alert_emails.find_one(
            {"_id": "guard_block:p1:risk_unknown"})
        assert rec is not None and rec["sends"] == 1
        # second identical block inside the cooldown does NOT double-record
        guard_alerts.queue_block_alert(db, snap)
        await asyncio.sleep(0.2)
        rec = await db.guard_alert_emails.find_one(
            {"_id": "guard_block:p1:risk_unknown"})
        assert rec["sends"] == 1
    finally:
        await db.client.drop_database(DB_NAME)


def test_guard_alert_cooldown_and_block_hook():
    asyncio.run(_alert_scenario())


async def _ops_alert_email_scenario():
    db = _fresh_db()
    try:
        # raise_alert(critical) queues the email hook exactly once per
        # unacked dedup_key
        from alerting import raise_alert
        rid = await raise_alert(db, "ea_heartbeat_stale", "critical",
                                "EA heartbeat 999s old",
                                dedup_key="ea_heartbeat:acct1")
        assert rid is not None
        await asyncio.sleep(0.2)
        rec = await db.guard_alert_emails.find_one(
            {"_id": "ops_email:ea_heartbeat:acct1"})
        assert rec is not None
        # duplicate unacked alert → no second insert, no second email
        rid2 = await raise_alert(db, "ea_heartbeat_stale", "critical",
                                 "EA heartbeat 1200s old",
                                 dedup_key="ea_heartbeat:acct1")
        assert rid2 is None
        await asyncio.sleep(0.2)
        rec = await db.guard_alert_emails.find_one(
            {"_id": "ops_email:ea_heartbeat:acct1"})
        assert rec["sends"] == 1
        # warning severity never emails
        await raise_alert(db, "outbox_backlog", "warning", "backlog",
                          dedup_key="outbox_backlog")
        await asyncio.sleep(0.2)
        assert await db.guard_alert_emails.find_one(
            {"_id": "ops_email:outbox_backlog"}) is None
    finally:
        await db.client.drop_database(DB_NAME)


def test_critical_ops_alert_emails():
    asyncio.run(_ops_alert_email_scenario())
