"""iter-122 Phase 3 — VPS agent trust + verified MT5 identity model.

Display labels are presentation-only; operational authority binds to
installation_id + broker_server + account_number:
  • verify_heartbeat_identity — full chain (installation recognized, bound
    to the account, server + login match, holds the execution lease),
  • heartbeats can never steal a lease held by another installation,
  • execution lease carries broker-verified identity,
  • authenticated monotonic command sequence + HMAC signatures + replay
    rejection,
  • signed artifact manifest (HMAC-SHA256, AGENT_SIGNING_KEY),
  • credential rotation + installation revocation endpoints.
"""
import asyncio
import hashlib
import hmac as hmac_mod
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

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


def _now():
    return datetime.now(timezone.utc)


async def _mk_account(db, uid, server="ICMarketsSC-Live27", number="12345678"):
    from bson import ObjectId
    aid = ObjectId()
    await db.accounts.insert_one({
        "_id": aid, "user_id": uid, "label": "My FTMO Account",  # display only
        "mode": "live", "server": server, "account_number": number,
        "bridge_token": f"tok-{uuid.uuid4().hex}",
        "created_at": _now().isoformat()})
    return await db.accounts.find_one({"_id": aid})


async def _mk_installation(db, uid, account_id, revoked=False):
    inst_id = f"inst_{uuid.uuid4().hex[:12]}"
    await db.installations.insert_one({
        "installation_id": inst_id, "user_id": uid, "account_id": account_id,
        "terminal_path": "C:/mt5/terminal64.exe", "host_fingerprint": "hostA",
        "revoked": revoked, "created_at": _now()})
    return inst_id


async def _grant_lease(db, account_id, inst_id, uid):
    await db.execution_leases.update_one(
        {"account_id": account_id},
        {"$set": {"installation_id": inst_id, "user_id": uid,
                  "revoked": False, "acquired_at": _now(),
                  "expires_at": _now() + timedelta(seconds=20)}}, upsert=True)


def test_heartbeat_identity_chain(svc_db):
    async def inner():
        from database import get_db
        from vps_agent import verify_heartbeat_identity
        db = get_db()
        uid = uuid.uuid4().hex
        acc = await _mk_account(db, uid)
        aid = str(acc["_id"])
        inst = await _mk_installation(db, uid, aid)
        await _grant_lease(db, aid, inst, uid)
        # full chain OK
        v = await verify_heartbeat_identity(
            db, acc, installation_id=inst,
            broker_server="ICMarketsSC-Live27", reported_login=12345678)
        assert v["ok"] is True
        # unknown installation
        v = await verify_heartbeat_identity(db, acc, installation_id="inst_bogus")
        assert v["ok"] is False and "unknown" in v["reason"]
        # broker server mismatch
        v = await verify_heartbeat_identity(
            db, acc, installation_id=inst, broker_server="OtherBroker-Demo01")
        assert v["ok"] is False and "server mismatch" in v["reason"]
        # login mismatch
        v = await verify_heartbeat_identity(
            db, acc, installation_id=inst, reported_login=99999999)
        assert v["ok"] is False and "login mismatch" in v["reason"]
        # installation bound to another account
        acc2 = await _mk_account(db, uid, number="222")
        v = await verify_heartbeat_identity(
            db, acc2, installation_id=inst)
        assert v["ok"] is False and "different account" in v["reason"]
        # not the lease owner
        inst2 = await _mk_installation(db, uid, aid)
        v = await verify_heartbeat_identity(db, acc, installation_id=inst2)
        assert v["ok"] is False and "lease" in v["reason"]
    _run(inner())


def test_heartbeat_cannot_steal_lease(svc_db):
    async def inner():
        from database import get_db
        from vps_agent import on_ea_heartbeat
        db = get_db()
        uid = uuid.uuid4().hex
        acc = await _mk_account(db, uid)
        aid = str(acc["_id"])
        owner = await _mk_installation(db, uid, aid)
        intruder = await _mk_installation(db, uid, aid)
        await _grant_lease(db, aid, owner, uid)
        # A heartbeat carrying the NON-owner installation must not renew/steal
        await on_ea_heartbeat(db, acc, 12345678, installation_id=intruder)
        lease = await db.execution_leases.find_one({"account_id": aid})
        assert lease["installation_id"] == owner
        # Owner heartbeat renews and stamps broker-verified identity
        await on_ea_heartbeat(db, acc, 12345678, installation_id=owner)
        lease = await db.execution_leases.find_one({"account_id": aid})
        assert lease["installation_id"] == owner
        assert lease["broker_server"] == "ICMarketsSC-Live27"
        assert lease["account_number"] == "12345678"
    _run(inner())


def test_command_sequence_signed_and_replay_rejected(svc_db):
    async def inner():
        from database import get_db
        from vps_pathb import queue_command, poll_commands, ack_command
        db = get_db()
        uid = uuid.uuid4().hex
        token = f"agt_tok_{uuid.uuid4().hex}"
        key = uuid.uuid4().hex * 2
        agent_id = f"agt_{uuid.uuid4().hex[:12]}"
        await db.vps_agents.insert_one({
            "agent_id": agent_id, "agent_token": token, "command_key": key,
            "command_seq": 0, "last_acked_seq": 0, "user_id": uid,
            "revoked": False, "registered_at": _now()})
        r1 = await queue_command(db, uid, agent_id, "run_diagnostics", {}, "t")
        r2 = await queue_command(db, uid, agent_id, "run_diagnostics", {}, "t")
        assert (r1["seq"], r2["seq"]) == (1, 2)
        delivered = await poll_commands(db, token)
        assert [d["seq"] for d in delivered] == [1, 2]
        for d in delivered:
            expected = hmac_mod.new(
                key.encode(),
                f"{agent_id}|{d['command_id']}|{d['seq']}|run_diagnostics".encode(),
                hashlib.sha256).hexdigest()
            assert d["sig"] == expected
        await ack_command(db, token, delivered[0]["command_id"], True)
        agent = await db.vps_agents.find_one({"agent_id": agent_id})
        assert agent["last_acked_seq"] == 1
        # replaying the SAME ack after completion is rejected
        with pytest.raises(ValueError):
            await ack_command(db, token, delivered[0]["command_id"], True)
    _run(inner())


def test_artifact_manifest_is_signed():
    from vps_pathb import build_artifact_manifest
    import release_signing
    m = build_artifact_manifest()
    sig = m["signature"]
    assert sig["alg"] == "Ed25519" and sig["value"]
    assert sig["key_id"] == release_signing.KEY_ID
    body = json.dumps({k: m[k] for k in sig["signed_fields"]},
                      sort_keys=True, separators=(",", ":"),
                      default=str).encode()
    assert release_signing.verify_hex(body, sig["value"],
                                      sig["public_key_b64"])
    # tampered body must fail verification
    assert not release_signing.verify_hex(body + b"x", sig["value"],
                                          sig["public_key_b64"])
    assert m["update_policy"]["rollback"]


def test_rotate_and_revoke_endpoints(svc_db):
    import requests
    from tests.helpers import register_and_login, make_elite, mongo_db
    API = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/") + "/api"
    email = f"iter122c-{uuid.uuid4().hex[:8]}@example.com"
    s = register_and_login(email)
    make_elite(email)
    db = mongo_db()
    uid = str(db.users.find_one({"email": email})["_id"])
    agent_id = f"agt_{uuid.uuid4().hex[:12]}"
    old_token = f"agt_tok_{uuid.uuid4().hex}"
    db.vps_agents.insert_one({
        "agent_id": agent_id, "agent_token": old_token,
        "command_key": "old", "user_id": uid, "revoked": False})
    r = s.post(f"{API}/infra/agents/{agent_id}/rotate-credentials", timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_token"] != old_token and body["command_key"] != "old"
    doc = db.vps_agents.find_one({"agent_id": agent_id})
    # iter-170 — token stored HASHED at rest, plaintext dropped
    from vps_agent import hash_agent_token
    assert "agent_token" not in doc
    assert doc["agent_token_hash"] == hash_agent_token(body["agent_token"])
    # installation revocation kills lease + rotates the bridge token
    from bson import ObjectId
    aid = db.accounts.insert_one({
        "user_id": uid, "label": "x", "mode": "live",
        "bridge_token": "tok_old", "server": "S", "account_number": "1",
        "created_at": datetime.now(timezone.utc).isoformat()}).inserted_id
    inst_id = f"inst_{uuid.uuid4().hex[:12]}"
    db.installations.insert_one({
        "installation_id": inst_id, "user_id": uid, "account_id": str(aid),
        "revoked": False, "created_at": datetime.now(timezone.utc)})
    db.execution_leases.update_one(
        {"account_id": str(aid)},
        {"$set": {"installation_id": inst_id, "user_id": uid,
                  "revoked": False,
                  "expires_at": datetime.now(timezone.utc)}}, upsert=True)
    r2 = s.post(f"{API}/infra/installations/{inst_id}/revoke", timeout=15)
    assert r2.status_code == 200, r2.text
    assert db.installations.find_one({"installation_id": inst_id})["revoked"] is True
    assert db.execution_leases.find_one({"account_id": str(aid)})["revoked"] is True
    assert db.accounts.find_one({"_id": aid})["bridge_token"] != "tok_old"
    # second revoke → 404 (idempotent surface)
    assert s.post(f"{API}/infra/installations/{inst_id}/revoke",
                  timeout=15).status_code == 404
