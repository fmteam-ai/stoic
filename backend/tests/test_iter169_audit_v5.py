"""Audit v5 (2026-08-30) corrections — governance + truth propagation.

Covers:
- P0-4 Loss Lab: declared gate enforced on revalidation; auto-apply
  suspended while truth is stale/unreconciled
- P1-3 synthetic identity auto-tagging in alerts
- P1-2 chaos per-scenario PARTIAL status
- P0-3 verified performance rows carry server-owned environment
"""
import asyncio
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

pytestmark = pytest.mark.integration


def _db():
    from motor.motor_asyncio import AsyncIOMotorClient
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestLossLabGate:
    def test_revalidation_reverts_below_gate_guard(self):
        async def go():
            db = _db()
            import loss_advisor as la
            uid = f"iter169_{uuid.uuid4().hex[:8]}"
            await db.auto_guards.insert_one({
                "user_id": uid, "active": True,
                "measure": {"type": "session_block", "title": "test guard",
                            "params": {"symbol": "XAUUSD", "action": "BUY",
                                       "session": "ny"}},
                "evidence": {"net_effect": 79.59, "losses_avoided": 300,
                             "wins_missed": 220, "testable": True}})
            orig = la._shadow_test
            la._shadow_test = lambda m, ds: {
                "net_effect": 79.59, "losses_avoided": 300,
                "wins_missed": 100, "testable": True}
            try:
                out = await la._revalidate_guards(db, uid, {})
            finally:
                la._shadow_test = orig
            g = await db.auto_guards.find_one({"user_id": uid})
            await db.auto_guards.delete_many({"user_id": uid})
            return out, g
        reverted, g = _run(go())
        assert len(reverted) == 1
        assert reverted[0]["reason"] == "evidence_below_declared_gate"
        assert g["active"] is False

    def test_no_active_guard_below_declared_gate_in_db(self):
        async def go():
            db = _db()
            from loss_advisor import AUTO_APPLY_MIN_NET, AUTO_APPLY_RATIO
            bad = []
            async for g in db.auto_guards.find({"active": True}):
                ev = g.get("latest_evidence") or g.get("evidence") or {}
                net = float(ev.get("net_effect") or 0)
                saved = float(ev.get("losses_avoided") or 0)
                missed = float(ev.get("wins_missed") or 0)
                if net < AUTO_APPLY_MIN_NET or saved < AUTO_APPLY_RATIO * missed:
                    bad.append((g.get("measure") or {}).get("title"))
            return bad
        assert _run(go()) == []

    def test_auto_apply_suspended_while_truth_stale(self):
        async def go():
            db = _db()
            from loss_advisor import _auto_apply_suspended
            u = await db.users.find_one({"email": "admin@trading.bot"})
            return await _auto_apply_suspended(db, str(u["_id"]))
        hold = _run(go())
        # preview truth is stale + 1 UNKNOWN execution — must be suspended
        assert hold in ("position_truth_stale", "reconciliation_pending",
                        "truth_unavailable"), hold


class TestSyntheticIdentityTagging:
    def test_known_test_identities_detected(self):
        from synthetic_data import mentions_synthetic_identity
        assert mentions_synthetic_identity(
            "worker lease expired on TEST_BridgeAcc")
        assert mentions_synthetic_identity("heartbeat missed for Paper#1")
        assert mentions_synthetic_identity("account spread-test degraded")
        assert mentions_synthetic_identity("compat failed reconcile")
        assert not mentions_synthetic_identity(
            "reconciliation backlog above threshold on RoboForex")

    def test_raise_alert_auto_tags(self):
        async def go():
            db = _db()
            from alerting import raise_alert
            key = f"iter169_{uuid.uuid4().hex[:8]}"
            await raise_alert(db, "test_kind", "warning",
                              "heartbeat missed for TEST_ScalpAcc",
                              dedup_key=key)
            a = await db.ops_alerts.find_one({"dedup_key": key})
            await db.ops_alerts.delete_many({"dedup_key": key})
            return a
        a = _run(go())
        assert a["synthetic"] is True


class TestChaosPartialStatus:
    def test_results_have_per_drill_status(self):
        async def go():
            from chaos_drills import run_drills
            return await run_drills(_db())
        out = _run(go())
        for r in out["results"]:
            assert r["status"] in ("PASS", "PARTIAL", "FAIL")
            if r["passed"] and r.get("skipped_assertions"):
                assert r["status"] == "PARTIAL"


class TestVerifiedEnvironment:
    def test_account_rows_carry_environment(self):
        async def go():
            db = _db()
            from routes.performance_routes import _verified_payload
            u = await db.users.find_one({"email": "admin@trading.bot"})
            return await _verified_payload(db, str(u["_id"]))
        p = _run(go())
        rows = p.get("accounts") or []
        assert rows, "expected account rows"
        for r in rows:
            assert r.get("environment") in (
                "LIVE", "DEMO", "PAPER", "UNKNOWN"), r
        # preview has NO live accounts — a demo record must never say LIVE
        assert all(r["environment"] != "LIVE" for r in rows), rows
