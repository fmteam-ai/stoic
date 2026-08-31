"""iter-179 — alerts AUTO-RESOLVE when their condition clears.

Evaluator-managed alerts (heartbeat, leases, outbox, unprotected positions,
reconciliation) must close themselves (acked_by=system:auto-resolved) once
the measured condition no longer holds, instead of capping Bot Health at 45
until a human acknowledges. Alerts raised by other subsystems are untouched.
"""
import asyncio
import datetime as dt
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
LEASE = "_t179_expired_lease"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _scenario():
    from motor.motor_asyncio import AsyncIOMotorClient
    from alerting import evaluate_ops_alerts, raise_alert

    db = AsyncIOMotorClient(MONGO_URL)[DB_NAME]
    now = dt.datetime.now(dt.timezone.utc)
    try:
        # condition CLEARED: open unprotected_positions alert, zero trades
        await raise_alert(db, "unprotected_positions", "critical",
                          "1 open position(s) without a broker-confirmed stop",
                          dedup_key="unprotected_positions")
        # condition ACTIVE: lease expired 10 min ago (within ceiling)
        await db.worker_leases.insert_one(
            {"_id": LEASE, "expires_at": now - dt.timedelta(minutes=10)})
        # foreign kind: must never be auto-resolved by the evaluator
        await raise_alert(db, "vps_disk_low", "warning",
                          "VPS disk nearly full", dedup_key="_t179_vps")

        await evaluate_ops_alerts(db)

        cleared = await db.ops_alerts.find_one(
            {"dedup_key": "unprotected_positions"})
        lease_alert = await db.ops_alerts.find_one(
            {"dedup_key": f"worker_lease:{LEASE}"})
        foreign = await db.ops_alerts.find_one({"dedup_key": "_t179_vps"})
        return cleared, lease_alert, foreign
    finally:
        await db.worker_leases.delete_many({"_id": LEASE})
        await db.ops_alerts.delete_many(
            {"dedup_key": {"$in": ["unprotected_positions",
                                   f"worker_lease:{LEASE}", "_t179_vps"]}})
        await db.email_outbox.delete_many(
            {"dedup_key": {"$in": ["unprotected_positions",
                                   f"worker_lease:{LEASE}"]}})


def test_auto_resolve_cleared_conditions_only():
    cleared, lease_alert, foreign = _run(_scenario())
    assert cleared is not None
    assert cleared.get("acked_at") is not None, "cleared condition not auto-resolved"
    assert cleared.get("acked_by") == "system:auto-resolved"
    assert cleared.get("auto_resolved") is True
    assert lease_alert is not None
    assert lease_alert.get("acked_at") is None, "active condition wrongly resolved"
    assert foreign is not None
    assert foreign.get("acked_at") is None, "foreign alert kind wrongly resolved"
