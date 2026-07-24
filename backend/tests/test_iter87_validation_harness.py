"""iter-87 — automated MT5 validation campaign harness."""
import asyncio
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient


def _server_up():
    try:
        return httpx.get("http://localhost:8001/api/health", timeout=3).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _server_up(),
                                reason="backend not running on :8001")


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def test_scenario_registry_covers_all_ledger_scenarios():
    from validation_harness import SCENARIO_IMPLS
    from routes.validation_routes import VALIDATION_SCENARIOS
    assert set(SCENARIO_IMPLS) == set(VALIDATION_SCENARIOS)


def test_harness_core_scenarios_pass_and_cleanup(db):
    from validation_harness import run_campaign, HARNESS_USER

    async def run():
        res = await run_campaign(
            db, "netting", record=False,
            scenarios=["duplicate_commands", "netting", "hedging",
                       "multi_deal_fills", "rejected_orders"])
        assert res["failed"] == 0, res["results"]
        assert res["passed"] == 5
        # cleanup — no harness residue
        assert await db.accounts.count_documents({"user_id": HARNESS_USER}) == 0
        assert await db.trades.count_documents({"user_id": HARNESS_USER}) == 0
        run = await db.validation_runs.find_one({"run_id": res["run_id"]})
        assert run and run["mode"] == "netting"
        await db.validation_runs.delete_one({"run_id": res["run_id"]})
    asyncio.get_event_loop().run_until_complete(run())


def test_record_true_writes_evidence(db):
    from validation_harness import run_campaign

    async def run():
        res = await run_campaign(db, "hedging", record=True,
                                 scenarios=["duplicate_commands"])
        ev = await db.validation_evidence.find_one(
            {"evidence_ref": f"harness:{res['run_id']}"})
        assert ev and ev["scenario"] == "duplicate_commands"
        assert ev["account_mode"] == "hedging"
        assert ev["status"] == "pass"
        await db.validation_runs.delete_one({"run_id": res["run_id"]})
    asyncio.get_event_loop().run_until_complete(run())
