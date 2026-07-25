"""iter-114 — pairing hardening: digest-only storage, atomic single-
claimant, one-terminal binding, bridge-token rotation, execution-owner
lease, real deployment state machine, EX5 artifact, no irm|iex."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient

from vps_agent import (DEPLOY_STATES, advance_ea_deployment,
                       claim_pairing_code, create_pairing_code,
                       on_ea_heartbeat)
from vps_pathb import build_artifact_manifest, connect_existing


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter114-{uuid.uuid4().hex[:8]}"
TERM = {"terminal_path": "C:\\STOIC\\MT5\\account-1\\",
        "host_fingerprint": "host-114"}


async def _account(db, login=None, server=None):
    res = await db.accounts.insert_one({
        "user_id": UID, "mode": "demo",
        "label": f"acct-{uuid.uuid4().hex[:6]}",
        "server": server,
        "broker_account_id_reported": login,
        "bridge_token": f"tok-{uuid.uuid4().hex}"})
    return str(res.inserted_id)


# ─── digest storage + terminal binding ──────────────────────────
def test_pairing_code_stored_as_digest_only(db):
    async def go():
        aid = await _account(db)
        p = await create_pairing_code(db, UID, aid)
        assert await db.ea_pairing_codes.find_one(
            {"code": p["code"]}) is None
        doc = await db.ea_pairing_codes.find_one({"account_id": aid})
        assert doc and len(doc["code_digest"]) == 64
        assert "code" not in doc
    _run(go())


def test_claim_requires_exactly_one_terminal(db):
    async def go():
        aid = await _account(db)
        p = await create_pairing_code(db, UID, aid)
        with pytest.raises(ValueError, match="exactly one MT5 terminal"):
            await claim_pairing_code(db, p["code"], {})
        with pytest.raises(ValueError, match="exactly one MT5 terminal"):
            await claim_pairing_code(db, p["code"],
                                     {"terminal_path": "C:\\x"})
        # code still claimable — rejection happened before consumption
        out = await claim_pairing_code(db, p["code"], TERM)
        assert out["installation_id"].startswith("inst_")
    _run(go())


def test_concurrent_claims_exactly_one_winner(db):
    async def go():
        aid = await _account(db)
        p = await create_pairing_code(db, UID, aid)

        async def try_claim(i):
            try:
                return await claim_pairing_code(
                    db, p["code"], {"terminal_path": f"C:\\t{i}\\",
                                    "host_fingerprint": f"h{i}"})
            except ValueError:
                return None
        results = await asyncio.gather(*[try_claim(i) for i in range(8)])
        winners = [r for r in results if r]
        assert len(winners) == 1
        assert await db.installations.count_documents(
            {"account_id": aid}) == 1
    _run(go())


def test_claim_rotates_bridge_token(db):
    async def go():
        aid = await _account(db)
        from bson import ObjectId
        before = (await db.accounts.find_one(
            {"_id": ObjectId(aid)}))["bridge_token"]
        p = await create_pairing_code(db, UID, aid)
        out = await claim_pairing_code(db, p["code"], TERM)
        after = (await db.accounts.find_one(
            {"_id": ObjectId(aid)}))["bridge_token"]
        assert after == out["bridge_token"] and after != before
        assert out["connected"] is False
    _run(go())


# ─── execution-owner lease ──────────────────────────────────────
def test_repair_blocked_while_lease_active(db):
    async def go():
        aid = await _account(db)
        p = await create_pairing_code(db, UID, aid)
        await claim_pairing_code(db, p["code"], TERM)  # acquires lease
        with pytest.raises(RuntimeError, match="active execution owner"):
            await create_pairing_code(db, UID, aid)
        # explicit revoke → allowed, old installation revoked
        p2 = await create_pairing_code(db, UID, aid,
                                       revoke_existing=True)
        assert p2["code"].startswith("PAIR-")
        old = await db.installations.find_one(
            {"account_id": aid, "revoked": True})
        assert old is not None
        lease = await db.execution_leases.find_one({"account_id": aid})
        assert lease["revoked"] is True
    _run(go())


def test_repair_allowed_after_lease_expiry(db):
    async def go():
        aid = await _account(db)
        p = await create_pairing_code(db, UID, aid)
        await claim_pairing_code(db, p["code"], TERM)
        await db.execution_leases.update_one(
            {"account_id": aid},
            {"$set": {"expires_at": datetime.now(timezone.utc)
                      - timedelta(seconds=5)}})
        p2 = await create_pairing_code(db, UID, aid)  # no revoke needed
        assert p2["code"].startswith("PAIR-")
    _run(go())


# ─── deployment state machine ───────────────────────────────────
def test_state_machine_full_ladder(db):
    async def go():
        aid = await _account(db, login="10001", server="Test-Live01")
        p = await create_pairing_code(db, UID, aid,
                                      expected_login="10001",
                                      expected_server="Test-Live01")
        dep = await db.ea_deployments.find_one({"account_id": aid})
        assert dep["state"] == "TOKEN_ISSUED"
        await claim_pairing_code(db, p["code"], TERM)
        dep = await db.ea_deployments.find_one({"account_id": aid})
        assert dep["state"] == "TERMINAL_SELECTED"
        await advance_ea_deployment(db, aid, "ARTIFACT_VERIFIED")
        await advance_ea_deployment(db, aid, "EA_INSTALLED")
        # forward-only: cannot go back
        await advance_ea_deployment(db, aid, "HOST_INSPECTED")
        dep = await db.ea_deployments.find_one({"account_id": aid})
        assert dep["state"] == "EA_INSTALLED"
        # heartbeat from expected account/server → READY_FOR_SHADOW
        from bson import ObjectId
        acc = await db.accounts.find_one({"_id": ObjectId(aid)})
        await on_ea_heartbeat(db, acc, reported_login="10001")
        dep = await db.ea_deployments.find_one({"account_id": aid})
        assert dep["state"] == "READY_FOR_SHADOW"
        states = [s["state"] for s in dep["state_history"]]
        assert "EA_HEARTBEAT_RECEIVED" in states
        assert "BROKER_ACCOUNT_VERIFIED" in states
        # lease renewed by the heartbeat
        lease = await db.execution_leases.find_one({"account_id": aid})
        assert lease and lease["revoked"] is False
    _run(go())


def test_wrong_login_never_reaches_ready(db):
    async def go():
        aid = await _account(db, login="20002", server="Test-Live01")
        p = await create_pairing_code(db, UID, aid,
                                      expected_login="20002")
        await claim_pairing_code(db, p["code"], TERM)
        from bson import ObjectId
        acc = await db.accounts.find_one({"_id": ObjectId(aid)})
        await on_ea_heartbeat(db, acc, reported_login="99999")
        dep = await db.ea_deployments.find_one({"account_id": aid})
        assert dep["state"] == "EA_HEARTBEAT_RECEIVED"
        assert dep["state"] != "READY_FOR_SHADOW"
    _run(go())


def test_deploy_states_order():
    assert DEPLOY_STATES[0] == "TOKEN_ISSUED"
    assert DEPLOY_STATES[-2:] == ["READY_FOR_SHADOW", "FAILED"]
    assert DEPLOY_STATES.index("EA_HEARTBEAT_RECEIVED") > \
        DEPLOY_STATES.index("EA_INSTALLED")


# ─── artifacts + no irm|iex ─────────────────────────────────────
def test_manifest_includes_ci_ex5(db):
    m = build_artifact_manifest()
    ex5 = next(a for a in m["artifacts"] if a["name"] == "stoic-ea-ex5")
    assert ex5["type"] == "ex5"
    assert "never recompile" in ex5["note"]


def test_connect_existing_has_no_iex_pipeline(db):
    async def go():
        out = await connect_existing(db, UID, {"label": "no-iex"})
        cmds = out["install_commands"]
        assert "quick" not in cmds
        joined = " ".join(cmds["recommended"])
        assert "| iex" not in joined and "irm " not in joined
        assert "Verify" in joined
    _run(go())


def test_install_ea_command_requires_terminal(db):
    async def go():
        from vps_pathb import queue_command
        await db.vps_agents.insert_one({
            "agent_id": f"agt_{UID}", "agent_token": f"tok_{UID}",
            "user_id": UID, "deployment_id": "dep-x", "revoked": False})
        with pytest.raises(ValueError, match="one terminal per account"):
            await queue_command(db, UID, f"agt_{UID}", "install_ea", {},
                                "user:test")
        q = await queue_command(db, UID, f"agt_{UID}", "install_ea",
                                {"terminal_path": "C:\\t\\",
                                 "account_ref": "10001"}, "user:test")
        assert q["status"] == "queued"
    _run(go())


# ─── Cleanup ────────────────────────────────────────────────────
def test_zz_cleanup(db):
    async def go():
        rx = {"$regex": f"^{UID}"}
        aids = [str(a["_id"]) async for a in
                db.accounts.find({"user_id": rx}, {"_id": 1})]
        for coll in ("accounts", "ea_pairing_codes", "ea_deployments",
                     "installations", "execution_leases", "vps_agents",
                     "agent_commands", "vps_deployments",
                     "vps_bootstrap_tokens"):
            await db[coll].delete_many({"user_id": rx})
        for coll in ("ea_deployments", "installations",
                     "execution_leases", "ea_pairing_codes"):
            await db[coll].delete_many({"account_id": {"$in": aids}})
    _run(go())
