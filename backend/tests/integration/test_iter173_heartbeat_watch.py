"""iter-173 — heartbeat/verification watch fires on TRANSITIONS only."""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

DB_NAME = f"stoic_test_hbwatch_{uuid.uuid4().hex[:8]}"


def _iso(seconds_ago=0):
    return (datetime.now(timezone.utc)
            - timedelta(seconds=seconds_ago)).isoformat()


async def _scenario():
    os.environ["DB_NAME"] = DB_NAME
    import database
    database._client = None
    from database import get_db
    from heartbeat_watch import check_once
    db = get_db()
    try:
        a = await db.accounts.insert_one(
            {"user_id": "hb_user", "label": "HBWatch", "mode": "live",
             "last_heartbeat": _iso(5),
             "ea_identity": {"installation_id": "inst_x",
                             "authoritative": True}})

        # 1st sweep records baseline (connected+verified) — no alerts
        out = await check_once(db)
        assert out["checked"] == 1 and out["alerts_fired"] == 0
        assert await db.ops_alerts.count_documents({}) == 0

        # heartbeat goes stale → exactly ONE heartbeat_lost alert
        await db.accounts.update_one(
            {"_id": a.inserted_id},
            {"$set": {"last_heartbeat": _iso(600)}})
        out = await check_once(db)
        assert out["alerts_fired"] == 1
        lost = await db.ops_alerts.find_one({"kind": "heartbeat_lost"})
        assert lost and "HBWatch" in lost["message"]
        # repeated sweep while still down → NO duplicate
        out = await check_once(db)
        assert out["alerts_fired"] == 0

        # recovery → informational restored alert, state re-armed
        await db.accounts.update_one(
            {"_id": a.inserted_id},
            {"$set": {"last_heartbeat": _iso(5)}})
        await check_once(db)
        restored = await db.ops_alerts.find_one(
            {"message": {"$regex": "restored"}})
        assert restored is not None

        # verification lost while connected → identity_lost alert
        await db.accounts.update_one(
            {"_id": a.inserted_id},
            {"$set": {"ea_identity.authoritative": False,
                      "ea_identity.reason": "installation revoked"}})
        out = await check_once(db)
        assert out["alerts_fired"] == 1
        ident = await db.ops_alerts.find_one({"kind": "identity_lost"})
        assert ident and "revoked" in ident["message"]

        # paper accounts are never watched
        await db.accounts.insert_one(
            {"user_id": "hb_user", "label": "Paper", "mode": "paper",
             "last_heartbeat": _iso(9999)})
        out = await check_once(db)
        assert out["checked"] == 1
    finally:
        await db.client.drop_database(DB_NAME)


def test_heartbeat_watch_transitions():
    asyncio.run(_scenario())
