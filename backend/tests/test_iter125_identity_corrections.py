"""iter-125 — "Most important remaining corrections" verification suite.

  #1  EA v1.55 mandates identity in heartbeats: no installation_id → no
      execution-lease renewal, live dispatch blocked by the identity gate.
  #2  Account identity structure: display_name / expected_identity /
      verified_identity; broker-reported values outrank user labels.
  #3  Mandatory artifact signing (missing AGENT_SIGNING_KEY = hard error)
      + CI-built EX5 delivery endpoint + digest report-back.
  #5  Broker-server matching via normalized exact alias registry (no
      substrings); affiliate DuplicateKeyError specificity + integer cents.
"""
import asyncio
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _now():
    return datetime.now(timezone.utc)


UID = f"iter125-{uuid.uuid4().hex[:8]}"


async def _mk_account(db, server="ICMarketsSC-Live", number="777001",
                      **extra):
    from bson import ObjectId
    aid = ObjectId()
    await db.accounts.insert_one({
        "_id": aid, "user_id": UID, "label": "User Label Only",
        "mode": "live", "server": server, "account_number": number,
        "bridge_token": f"tok-{uuid.uuid4().hex}",
        "created_at": _now().isoformat(), **extra})
    return await db.accounts.find_one({"_id": aid})


# ─── #5.1 broker-server alias registry (exact, never substring) ──────
def test_servers_match_exact_and_aliases():
    from broker_servers import servers_match, normalize_server
    assert servers_match("ICMarketsSC-Live", "icmarketssc-live")
    assert servers_match(" RoboForex-Pro ", "roboforex-pro")
    # alias group membership
    assert servers_match("RoboForex-Pro", "RoboForex-Prime")
    assert servers_match("ICMarketsSC-Live", "ICMarkets-Live")
    # SUBSTRING MATCHING IS DEAD: prefixes/lookalikes must NOT match
    assert not servers_match("IC", "ICMarketsSC-Live")
    assert not servers_match("ICMarketsSC-Live", "NicMarketsSC-Live-Fake")
    assert not servers_match("MetaQuotes-Demo", "MetaQuotes-Demo2")
    assert not servers_match("", "ICMarketsSC-Live")
    # db-provided extra alias groups
    assert servers_match("BrokerX-Live01", "BrokerX-Live-EU",
                         extra_aliases=[["BrokerX-Live01", "BrokerX-Live-EU"]])
    assert normalize_server("  Foo   Bar ") == "foo bar"


def test_verify_heartbeat_identity_rejects_lookalike_server(db):
    async def go():
        from vps_agent import verify_heartbeat_identity
        acc = await _mk_account(db)
        aid = str(acc["_id"])
        inst = f"inst_{uuid.uuid4().hex[:12]}"
        await db.installations.insert_one({
            "installation_id": inst, "user_id": UID, "account_id": aid,
            "terminal_path": "C:/mt5", "host_fingerprint": "h",
            "revoked": False, "created_at": _now()})
        await db.execution_leases.update_one(
            {"account_id": aid},
            {"$set": {"installation_id": inst, "user_id": UID,
                      "revoked": False,
                      "expires_at": _now() + timedelta(seconds=20)}},
            upsert=True)
        # pre-fix behaviour: substring "ICMarketsSC-Live" in
        # "ICMarketsSC-Live-Phishy" would PASS. Now it must fail.
        v = await verify_heartbeat_identity(
            db, acc, installation_id=inst,
            broker_server="ICMarketsSC-Live-Phishy", reported_login=777001)
        assert v["ok"] is False and "mismatch" in v["reason"]
        # exact (case-insensitive) still verifies
        v = await verify_heartbeat_identity(
            db, acc, installation_id=inst,
            broker_server="icmarketssc-live", reported_login=777001)
        assert v["ok"] is True
    _run(go())


# ─── #1 unidentified heartbeats never renew the lease ────────────────
def test_unidentified_heartbeat_never_renews_lease(db):
    async def go():
        from vps_agent import on_ea_heartbeat
        acc = await _mk_account(db)
        aid = str(acc["_id"])
        inst = f"inst_{uuid.uuid4().hex[:12]}"
        await db.installations.insert_one({
            "installation_id": inst, "user_id": UID, "account_id": aid,
            "terminal_path": "C:/mt5", "host_fingerprint": "h",
            "revoked": False, "created_at": _now()})
        stale = _now() - timedelta(seconds=60)
        await db.execution_leases.update_one(
            {"account_id": aid},
            {"$set": {"installation_id": inst, "user_id": UID,
                      "revoked": False, "expires_at": stale}}, upsert=True)
        # legacy heartbeat (no installation_id) — MUST NOT renew
        await on_ea_heartbeat(db, acc, reported_login="777001")
        lease = await db.execution_leases.find_one({"account_id": aid})
        assert lease["expires_at"].replace(tzinfo=timezone.utc) <= _now()
        # identified heartbeat from the owner — renews
        await on_ea_heartbeat(db, acc, reported_login="777001",
                              installation_id=inst,
                              broker_server="ICMarketsSC-Live")
        lease = await db.execution_leases.find_one({"account_id": aid})
        assert lease["expires_at"].replace(tzinfo=timezone.utc) > _now()
    _run(go())


# ─── #1 live dispatch identity gate ──────────────────────────────────
def test_execution_identity_gate(db):
    async def go():
        from vps_agent import verify_execution_identity
        # paper accounts are exempt
        assert await verify_execution_identity(
            db, {"_id": "x", "mode": "paper"}) is None
        # live account, no verified identity → blocked
        acc = await _mk_account(db)
        block = await verify_execution_identity(db, acc)
        assert block and block["blocked"] == "identity"
        assert "v1.55" in block["reason"]
        # unauthoritative identity → still blocked
        acc["ea_identity"] = {"installation_id": "inst_a",
                              "authoritative": False, "reason": "mismatch"}
        block = await verify_execution_identity(db, acc)
        assert block and block["blocked"] == "identity"
        # authoritative identity but NO lease → blocked
        acc["ea_identity"] = {"installation_id": "inst_a",
                              "authoritative": True}
        block = await verify_execution_identity(db, acc)
        assert block and "lease" in block["reason"]
        # lease held by ANOTHER installation → blocked
        aid = str(acc["_id"])
        await db.execution_leases.update_one(
            {"account_id": aid},
            {"$set": {"installation_id": "inst_other", "revoked": False,
                      "expires_at": _now() + timedelta(seconds=20)}},
            upsert=True)
        block = await verify_execution_identity(db, acc)
        assert block and "lease" in block["reason"]
        # verified identity + owned unexpired lease → allowed
        await db.execution_leases.update_one(
            {"account_id": aid},
            {"$set": {"installation_id": "inst_a"}})
        assert await verify_execution_identity(db, acc) is None
        # expired lease → blocked again
        await db.execution_leases.update_one(
            {"account_id": aid},
            {"$set": {"expires_at": _now() - timedelta(seconds=1)}})
        block = await verify_execution_identity(db, acc)
        assert block and block["blocked"] == "identity"
    _run(go())


# ─── #2 identity structure ───────────────────────────────────────────
def test_authoritative_account_number_precedence():
    from identity_model import authoritative_account_number
    acc = {"account_number": "user-entered",
           "expected_identity": {"account_number": "expected-claim"},
           "broker_account_id_reported": "broker-reported",
           "verified_identity": {"account_number": "verified"}}
    assert authoritative_account_number(acc) == "verified"
    del acc["verified_identity"]
    assert authoritative_account_number(acc) == "broker-reported"
    del acc["broker_account_id_reported"]
    assert authoritative_account_number(acc) == "expected-claim"
    del acc["expected_identity"]
    assert authoritative_account_number(acc) == "user-entered"


def test_backfill_identity_structure(db):
    async def go():
        from identity_model import backfill_identity_structure
        acc = await _mk_account(db, server="RoboForex-Pro", number="555")
        await backfill_identity_structure(db)
        fresh = await db.accounts.find_one({"_id": acc["_id"]})
        assert fresh["display_name"] == "User Label Only"
        assert fresh["expected_identity"] == {
            "account_number": "555", "broker_server": "RoboForex-Pro"}
        # idempotent — second run leaves it untouched
        await db.accounts.update_one(
            {"_id": acc["_id"]}, {"$set": {"display_name": "Renamed"}})
        await backfill_identity_structure(db)
        fresh = await db.accounts.find_one({"_id": acc["_id"]})
        assert fresh["display_name"] == "Renamed"
    _run(go())


def test_broker_identity_snapshot_prefers_verified():
    from execution import broker_identity_snapshot
    snap = broker_identity_snapshot({
        "account_number": "label-num", "server": "user-server",
        "broker": "IC Markets",
        "broker_account_id_reported": "8123",
        "ea_identity": {"installation_id": "inst_old"},
        "verified_identity": {"account_number": "9999",
                              "broker_server": "ICMarketsSC-Live",
                              "installation_id": "inst_new"}})
    assert snap["account_number"] == "9999"
    assert snap["broker_server"] == "ICMarketsSC-Live"
    assert snap["installation_id"] == "inst_new"


def test_pairing_claim_prefers_broker_reported(db):
    async def go():
        from vps_agent import create_pairing_code, claim_pairing_code
        acc = await _mk_account(
            db, number="user-num",
            broker_account_id_reported="broker-num")
        p = await create_pairing_code(db, UID, str(acc["_id"]))
        claim = await claim_pairing_code(
            db, p["code"], {"terminal_path": "C:/mt5",
                            "host_fingerprint": "h125"})
        assert claim["permitted_account"] == "broker-num"
    _run(go())


# ─── #3 mandatory artifact signing + EX5 delivery ────────────────────
def test_manifest_refuses_without_signing_key(monkeypatch):
    # iter-136 upgraded HMAC (AGENT_SIGNING_KEY) → Ed25519 asymmetric keys.
    from vps_pathb import build_artifact_manifest
    monkeypatch.delenv("ED25519_SIGNING_KEY_B64", raising=False)
    with pytest.raises(RuntimeError, match="ED25519_SIGNING_KEY_B64"):
        build_artifact_manifest()
    import base64
    from cryptography.hazmat.primitives import serialization as _s
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey as _EK)
    key_b64 = base64.b64encode(_EK.generate().private_bytes(
        _s.Encoding.Raw, _s.PrivateFormat.Raw, _s.NoEncryption())).decode()
    monkeypatch.setenv("ED25519_SIGNING_KEY_B64", key_b64)
    m = build_artifact_manifest()
    assert m["signature"]["value"]  # never null
    ex5 = next(a for a in m["artifacts"] if a["name"] == "stoic-ea-ex5")
    assert (ex5["url"] == "/api/ea-script.ex5"
            or re.fullmatch(r"/api/artifacts/[0-9a-f]{64}", ex5["url"]))


def test_ex5_endpoint_and_digest_report(db):
    import httpx
    base = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
    r = httpx.get(f"{base}/api/ea-script.ex5", timeout=20,
                  follow_redirects=True)
    # no CI binary published in this environment → explicit 409, never a
    # silent 404/None; when published, must carry the digest header.
    if r.status_code == 200:
        assert r.headers.get("x-stoic-sha256")
    else:
        assert r.status_code == 409
        assert r.json()["error"] == "ex5_not_published"
    # digest report requires a token
    r = httpx.post(f"{base}/api/infra/agent/artifact-digest",
                   json={"artifact": "stoic-ea-ex5", "sha256": "ab" * 32},
                   timeout=20)
    assert r.status_code == 401
    r = httpx.post(f"{base}/api/infra/agent/artifact-digest",
                   json={"bridge_token": "tok-bogus",
                         "artifact": "stoic-ea-ex5", "sha256": "ab" * 32},
                   timeout=20)
    assert r.status_code == 401

    async def go():
        acc = await _mk_account(db)
        r2 = httpx.post(f"{base}/api/infra/agent/artifact-digest",
                        json={"bridge_token": acc["bridge_token"],
                              "artifact": "stoic-ea",
                              "sha256": "de" * 32, "version": "1.55"},
                        timeout=20)
        assert r2.status_code == 200
        body = r2.json()
        assert body["match"] is False  # wrong digest detected
        row = await db.artifact_digests.find_one(
            {"account_id": str(acc["_id"])})
        assert row and row["match"] is False
    _run(go())


# ─── #1 EA v1.55 payload coherence ───────────────────────────────────
def test_ea_155_sends_identity_block():
    from ea_version import current_ea_version, EA_PATH
    assert current_ea_version() >= "1.55"
    src = open(EA_PATH).read()
    assert '"installation_id\\":\\"%s\\"' in src.replace("'", '"') or \
        '\\"installation_id\\":\\"%s\\"' in src
    assert "terminal_build" in src
    assert "ResolveInstallationId" in src
    assert "STOIC-Installation.txt" in src
    assert "TERMINAL_BUILD" in src


def test_installer_writes_installation_and_verifies_ex5():
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "static", "STOIC-Installer.ps1")
    src = open(path).read()
    assert "STOIC-Installation.txt" in src
    assert "/api/ea-script.ex5" in src
    assert "X-STOIC-SHA256" in src
    assert "artifact-digest" in src


# ─── #5.2 / #5.3 affiliate: DuplicateKeyError + integer cents ────────
def test_commission_uses_integer_cents(db):
    async def go():
        import database
        database._client = None
        database._db = None
        from affiliate_service import record_commission_if_referred
        from bson import ObjectId
        real_db = database.get_db()
        buyer_id = ObjectId()
        aff_uid = uuid.uuid4().hex
        code = f"ITER125{uuid.uuid4().hex[:4].upper()}"
        await real_db.users.insert_one({
            "_id": buyer_id, "email": f"{uuid.uuid4().hex[:6]}@t.io",
            "referred_by_code": code,
            "referred_at": _now().isoformat()})
        await real_db.affiliates.insert_one({
            "user_id": aff_uid, "code": code, "active": True})
        session = f"cs_{uuid.uuid4().hex}"
        doc = await record_commission_if_referred(
            user_id=str(buyer_id), plan_id="trader_monthly",
            amount_usd=99.99, session_id=session)
        assert doc is not None
        assert isinstance(doc["commission_cents"], int)
        assert isinstance(doc["sale_amount_cents"], int)
        assert doc["sale_amount_cents"] == 9999
        assert doc["commission_cents"] == round(
            doc["commission_usd"] * 100)
        aff = await real_db.affiliates.find_one({"code": code})
        assert aff["unpaid_balance_cents"] == doc["commission_cents"]
        assert isinstance(aff["unpaid_balance_cents"], int)
        # replay of the same session → duplicate swallowed, no double credit
        doc2 = await record_commission_if_referred(
            user_id=str(buyer_id), plan_id="trader_monthly",
            amount_usd=99.99, session_id=session)
        assert doc2 is None
        aff = await real_db.affiliates.find_one({"code": code})
        assert aff["unpaid_balance_cents"] == doc["commission_cents"]
        database._client = None
        database._db = None
    _run(go())


def test_only_duplicatekey_is_swallowed():
    import inspect
    import re
    from affiliate_service import record_commission_if_referred
    src = inspect.getsource(record_commission_if_referred)
    # the commission INSERT is guarded by DuplicateKeyError specifically —
    # network/DB failures propagate to the outbox retry loop
    m = re.search(r"insert_one\(doc\)\s+except (\w+)", src)
    assert m and m.group(1) == "DuplicateKeyError"


def test_reversal_uses_integer_cents(db):
    async def go():
        from affiliate_service import reverse_commissions_for_session
        from bson import ObjectId
        aff_id = ObjectId()
        await db.affiliates.insert_one({
            "_id": aff_id, "user_id": uuid.uuid4().hex,
            "code": f"REV{uuid.uuid4().hex[:5].upper()}", "active": True,
            "lifetime_earnings_cents": 3000, "unpaid_balance_cents": 3000,
            "lifetime_earnings_usd": 30.0, "unpaid_balance_usd": 30.0})
        session = f"cs_{uuid.uuid4().hex}"
        await db.affiliate_commissions.insert_one({
            "affiliate_id": str(aff_id), "session_id": session, "tier": 1,
            "commission_cents": 3000, "commission_usd": 30.0,
            "status": "pending", "created_at": _now().isoformat()})
        n = await reverse_commissions_for_session(db, session)
        assert n == 1
        aff = await db.affiliates.find_one({"_id": aff_id})
        assert aff["unpaid_balance_cents"] == 0
        assert aff["lifetime_earnings_cents"] == 0
    _run(go())


# ─── #2 account creation stamps the identity structure ──────────────
def test_account_create_route_writes_identity_structure():
    import inspect
    from routes import account_routes
    src = inspect.getsource(account_routes)
    assert '"display_name": payload.label' in src
    assert '"expected_identity"' in src


def test_heartbeat_persists_verified_identity():
    import inspect
    from routes import bridge_routes
    src = inspect.getsource(bridge_routes)
    assert "build_verified_identity" in src
    assert 'set_doc["verified_identity"]' in src
    # v1.55+ heartbeat without installation_id → explicitly unverified
    assert "missing installation_id" in src


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
