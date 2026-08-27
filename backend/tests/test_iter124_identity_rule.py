"""iter-124 — Identity rule codified: user-entered labels are presentation
only; broker-verified identity (account_number + broker_server +
installation_id) is authoritative everywhere trading identity matters.
See /app/docs/IDENTITY_MODEL.md.
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def svc_db():
    import database
    database._client = None
    database._db = None
    yield
    database._client = None
    database._db = None


def test_broker_identity_snapshot_never_uses_label():
    from execution import broker_identity_snapshot
    acc = {"label": "My FTMO Account", "account_number": "12345678",
           "server": "ICMarketsSC-Live27", "broker": "IC Markets",
           "broker_account_id_reported": "12345678",
           "ea_identity": {"installation_id": "inst_abc"}}
    snap = broker_identity_snapshot(acc)
    assert snap["account_number"] == "12345678"
    assert snap["broker_server"] == "ICMarketsSC-Live27"
    assert snap["installation_id"] == "inst_abc"
    assert "label" not in snap and "My FTMO Account" not in str(snap.values())
    # Broker-REPORTED login wins over the user-typed expectation
    acc["broker_account_id_reported"] = "99999999"
    assert broker_identity_snapshot(acc)["account_number"] == "99999999"


def test_pairing_permitted_account_is_broker_number(svc_db):
    async def inner():
        from bson import ObjectId
        from database import get_db
        from vps_agent import create_pairing_code, claim_pairing_code
        db = get_db()
        uid = uuid.uuid4().hex
        aid = ObjectId()
        await db.accounts.insert_one({
            "_id": aid, "user_id": uid, "label": "Gold Account",  # display only
            "mode": "live", "server": "ICMarketsSC-Live27",
            "account_number": "12345678",
            "bridge_token": f"tok-{uuid.uuid4().hex}",
            "created_at": datetime.now(timezone.utc).isoformat()})
        p = await create_pairing_code(db, uid, str(aid))
        out = await claim_pairing_code(db, p["code"], {
            "terminal_path": "C:/mt5/terminal64.exe",
            "host_fingerprint": "hostX"})
        assert out["permitted_account"] == "12345678"
        assert out["permitted_account"] != "Gold Account"
    _run(inner())


def test_promotion_evidence_carries_broker_identity(svc_db):
    async def inner():
        from bson import ObjectId
        from database import get_db
        from operational_modes import promotion_gate
        db = get_db()
        uid = uuid.uuid4().hex
        aid = ObjectId()
        await db.accounts.insert_one({
            "_id": aid, "user_id": uid, "label": "Renamed Later",
            "mode": "live", "server": "ICMarketsSC-Live27",
            "account_number": "87654321", "broker": "IC Markets",
            "bridge_token": f"tok-{uuid.uuid4().hex}",
            "created_at": datetime.now(timezone.utc).isoformat()})
        verdict = await promotion_gate(db, uid, str(aid), "supervised_live")
        certs = verdict["evidence"]["certifications"]
        assert len(certs) == 1
        c = certs[0]
        assert c["account_number"] == "87654321"
        assert c["account_id"] == str(aid)
        assert c["broker_server"] == "ICMarketsSC-Live27"
        assert c["account"] == "87654321"       # operational key = broker number
        assert c["account_label"] == "Renamed Later"  # display only
    _run(inner())


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
