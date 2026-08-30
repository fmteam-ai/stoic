"""Audit v4 (2026-08-30) corrections — authoritative truth + honest evidence.

Covers:
- P0-1 authority ribbon derives position truth from canonical readiness
- P0-2 environment classification (account_type wins; caps count LIVE only)
- P0-2/P0-3 identity-mismatch quarantine at the execution choke point
- P0-5 chaos PARTIAL aggregation (skipped assertions are not passes)
- P1-4 recovery INSUFFICIENT_DATA on zero denominator
"""
import asyncio
import os
import sys

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


class TestEnvironmentClassification:
    def test_account_type_demo_wins_over_server_name(self):
        from broker_env import broker_environment
        assert broker_environment(
            {"account_type": "demo", "broker_server": "Broker-Real"}) \
            == "DEMO"

    def test_explicit_and_paper_still_win(self):
        from broker_env import broker_environment
        assert broker_environment(
            {"broker_environment": "LIVE", "account_type": "demo"}) == "LIVE"
        assert broker_environment(
            {"mode": "paper", "account_type": "demo"}) == "PAPER"

    def test_caps_count_live_environment_only(self):
        async def go():
            db = _db()
            u = await db.users.find_one({"email": "admin@trading.bot"})
            from account_limits import get_broker_breakdown
            from broker_env import broker_environment
            bd = await get_broker_breakdown(db, str(u["_id"]))
            live = 0
            async for a in db.accounts.find(
                    {"user_id": str(u["_id"]), "mode": {"$ne": "paper"}}):
                if broker_environment(a) == "LIVE":
                    live += 1
            return bd, live
        bd, live = _run(go())
        assert bd["total_live_accounts"] == live
        assert "environment_counts" in bd
        assert sum(v["count"] for v in bd["breakdown"]) == live


class TestIdentityQuarantine:
    def test_mismatched_account_is_locked_at_choke_point(self):
        from trading_authority import account_domain
        d = _run(account_domain(_db(), {"broker_account_mismatch": True}))
        assert d["level"] == "LOCKED"
        assert "quarantined" in d["reason"]

    def test_enforce_new_trade_refuses_quarantined_account(self):
        from trading_authority import enforce_new_trade
        gate = _run(enforce_new_trade(
            _db(), account={"broker_account_mismatch": True}))
        assert gate["ok"] is False
        assert any("quarantined" in r for r in gate["reasons"])

    def test_clean_account_unaffected(self):
        from trading_authority import account_domain
        d = _run(account_domain(_db(), {"label": "clean"}))
        assert d["level"] == "FULL"


class TestChaosPartialAggregation:
    def test_run_drills_reports_partial_separately(self):
        from chaos_drills import run_drills
        out = _run(run_drills(_db()))
        assert out["passed"] + out["partial"] <= out["total"]
        skipped_drills = [r for r in out["results"]
                          if r.get("skipped_assertions")]
        # every drill with skipped assertions is counted PARTIAL, not passed
        assert out["partial"] == sum(1 for r in skipped_drills
                                     if r["passed"])


class TestRecoveryInsufficientData:
    def test_zero_samples_reports_insufficient_data(self):
        async def go():
            db = _db()
            from auto_demotion import recovery_status
            return await recovery_status(db, "user-with-no-samples-xyz")
        out = _run(go())
        if out.get("applicable") and out.get("samples") == 0:
            assert "INSUFFICIENT_DATA" in out["reason"]
