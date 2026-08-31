"""iter-177 — Bot Health 45 loop regression.

A worker lease that expired weeks ago (decommissioned worker, e.g. preview
single-process mode) must NOT re-raise a critical `worker_lease_expired`
alert after every acknowledge. Leases expired beyond
WORKER_LEASE_ALERT_MAX_AGE_SEC (default 24h) are skipped entirely; leases
expired within the ceiling still alert.
"""
import asyncio
import datetime as dt
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

DEAD = "_t177_dead_lease"
FRESH = "_t177_fresh_expired_lease"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _scenario():
    from motor.motor_asyncio import AsyncIOMotorClient
    from alerting import evaluate_ops_alerts

    db = AsyncIOMotorClient(MONGO_URL)[DB_NAME]
    now = dt.datetime.now(dt.timezone.utc)
    try:
        await db.worker_leases.insert_one(
            {"_id": DEAD, "expires_at": now - dt.timedelta(days=30)})
        await db.worker_leases.insert_one(
            {"_id": FRESH, "expires_at": now - dt.timedelta(minutes=10)})
        await evaluate_ops_alerts(db)
        dead_alert = await db.ops_alerts.find_one(
            {"dedup_key": f"worker_lease:{DEAD}"})
        fresh_alert = await db.ops_alerts.find_one(
            {"dedup_key": f"worker_lease:{FRESH}"})
        return dead_alert, fresh_alert
    finally:
        await db.worker_leases.delete_many({"_id": {"$in": [DEAD, FRESH]}})
        await db.ops_alerts.delete_many(
            {"dedup_key": {"$in": [f"worker_lease:{DEAD}",
                                   f"worker_lease:{FRESH}"]}})
        await db.email_outbox.delete_many(
            {"dedup_key": {"$in": [f"worker_lease:{DEAD}",
                                   f"worker_lease:{FRESH}"]}})


def test_dead_lease_skipped_fresh_lease_alerts():
    dead_alert, fresh_alert = _run(_scenario())
    assert dead_alert is None, (
        f"decommissioned lease still raised an alert: {dead_alert}")
    assert fresh_alert is not None, (
        "lease expired within the ceiling must still raise an alert")
    assert fresh_alert["severity"] == "critical"
