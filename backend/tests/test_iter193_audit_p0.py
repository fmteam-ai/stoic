"""iter-193 — audit corrections P0-1/P0-2/P1-1/P1-2/P1-3/P1-6/P0-3 (HTTP).

1. /api/authority: enforced_level == level, authority_state KNOWN.
2. /api/health exposes provenance (build_sha, policy + EA versions).
3. Attestation gate: a fresh user (no LIVE data) is REFUSED a public
   share (409 attestation_gate_failed); an eligible user succeeds and the
   public page reports VERIFIED.
4. trust-stats carries the live-label contract (as_of, source, ttl).
5. trading_enabled: missing flag reads DISABLED in the state contract.
"""
import asyncio
import os
import uuid

import requests
from pymongo import MongoClient

from helpers import (cleanup_attestation_user, register_and_login,
                     seed_attestation_eligible_user)

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
TAG = uuid.uuid4().hex[:8]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_authority_route_single_decision():
    s = register_and_login(f"auth_{TAG}@example.com")
    try:
        r = s.get(f"{BASE}/api/authority", timeout=30)
        assert r.status_code == 200, r.text[:200]
        b = r.json()
        assert b["authority_state"] == "KNOWN"
        assert b["enforced_level"] == b["level"]
        assert set(b["domains"]) >= {"platform", "broker", "risk", "pamm",
                                     "execution", "position_truth",
                                     "infrastructure", "account"}
    finally:
        cleanup_attestation_user(f"auth_{TAG}@example.com")


def test_health_provenance():
    b = requests.get(f"{BASE}/api/health", timeout=30).json()
    for k in ("build_sha", "execution_policy_version", "ea_version",
              "app_env"):
        assert k in b, k
    assert b["execution_policy_version"].startswith("v")


def test_share_refused_without_attestation_then_allowed_when_eligible():
    plain = register_and_login(f"noattest_{TAG}@example.com")
    try:
        r = plain.post(f"{BASE}/api/performance/share", timeout=30)
        assert r.status_code == 409, r.text[:300]
        d = r.json()["detail"]
        assert d["code"] == "attestation_gate_failed"
        assert "NO_BROKER_DEALS" in d["reasons"]
        assert "NO_ENABLED_ACCOUNTS" in d["reasons"]
    finally:
        cleanup_attestation_user(f"noattest_{TAG}@example.com")

    ok = seed_attestation_eligible_user(f"attest_{TAG}@example.com")
    try:
        v = ok.get(f"{BASE}/api/performance/verified", timeout=30).json()
        assert v["attestation"] and v["attestation_blocked"] is None
        assert v["attestation_policy_version"] == "attest-v2"
        r = ok.post(f"{BASE}/api/performance/share", timeout=30)
        assert r.status_code == 200, r.text[:300]
        sid = r.json()["share_id"]
        pub = requests.get(f"{BASE}/api/public/performance/{sid}",
                           timeout=30).json()
        assert pub["verification_status"] == "VERIFIED"
        assert pub["overall"] is not None
        # gate later fails (account flips to DEMO) → public page strips
        # headline metrics and labels UNVERIFIED
        db = MongoClient(MONGO_URL)[DB_NAME]
        uid = str(db.users.find_one(
            {"email": f"attest_{TAG}@example.com"})["_id"])
        db.accounts.update_many({"user_id": uid},
                                {"$set": {"broker_environment": "DEMO"}})
        pub2 = requests.get(f"{BASE}/api/public/performance/{sid}",
                            timeout=30).json()
        assert pub2["verification_status"].startswith("UNVERIFIED")
        assert pub2["overall"] is None and pub2["equity_curve"] == []
        assert any(x.startswith("NON_LIVE_ENVIRONMENT")
                   for x in pub2["attestation_blocked"]["reasons"])
    finally:
        cleanup_attestation_user(f"attest_{TAG}@example.com")


def test_trust_stats_live_label_contract():
    b = requests.get(f"{BASE}/api/public/trust-stats", timeout=30).json()
    for k in ("as_of", "source", "environment", "population",
              "ttl_seconds"):
        assert k in b, k


def test_missing_trading_enabled_reads_disabled():
    from motor.motor_asyncio import AsyncIOMotorClient
    from state_contract import contract, backfill_trading_enabled

    async def scenario():
        db = AsyncIOMotorClient(MONGO_URL)[DB_NAME]
        uid = f"u_te_{TAG}"
        await db.accounts.insert_one(
            {"user_id": uid, "label": f"TEST_te_{TAG}", "mode": "paper",
             "status": "connected"})
        try:
            sc = await contract(db, uid)
            assert sc["accounts"][0]["account_enabled"] is False
            assert sc["totals"]["accounts_enabled"] == 0
            n = await backfill_trading_enabled(db)
            assert n >= 1
            a = await db.accounts.find_one({"user_id": uid})
            assert a["trading_enabled"] is False   # no active bot config
            assert a.get("trading_enabled_backfilled_at")
        finally:
            await db.accounts.delete_many({"user_id": uid})
    _run(scenario())


def _uid(db, email):
    return str(db.users.find_one({"email": email})["_id"])


def test_attestation_gate_populations():
    """audit v5 P1-3 — population matrix on a real user:
    mixed enabled/disabled rows · demo+live · UNKNOWN (unverified) live ·
    stale newest deal. Disabled rows never influence the verdict."""
    import time
    from bson import ObjectId
    email = f"pop_{TAG}@example.com"
    s = seed_attestation_eligible_user(email)
    db = MongoClient(MONGO_URL)[DB_NAME]
    uid = _uid(db, email)

    def gate():
        return s.get(f"{BASE}/api/performance/verified",
                     timeout=30).json().get("attestation_blocked")
    try:
        assert gate() is None                      # baseline eligible

        # a DISABLED demo row must not affect the verdict
        demo_id = ObjectId()
        db.accounts.insert_one({"_id": demo_id, "user_id": uid,
                                "label": "Demo (off)", "server": "X-Demo",
                                "mode": "live", "trading_enabled": False,
                                "status": "connected",
                                "bridge_token": f"qa_{TAG}_demo"})
        assert gate() is None

        # ENABLE the demo row → demo + live population is refused
        db.accounts.update_one({"_id": demo_id},
                               {"$set": {"trading_enabled": True}})
        b = gate()
        assert b and any(r.startswith("NON_LIVE_ENVIRONMENT") and "DEMO" in r
                         for r in b["reasons"])
        db.accounts.delete_one({"_id": demo_id})

        # LIVE-classified but NOT identity-verified → UNKNOWN → refused
        db.accounts.update_many({"user_id": uid},
                                {"$unset": {"verified_identity": ""}})
        b = gate()
        assert b and any("UNKNOWN" in r for r in b["reasons"])
        db.accounts.update_many({"user_id": uid}, {"$set": {
            "verified_identity": {"account_number": "1",
                                  "broker_server": "QABroker-Live"}}})
        assert gate() is None

        # newest deal older than 6h → stale
        db.broker_deals.update_many(
            {"user_id": uid},
            {"$set": {"deal_time": int(time.time()) - 7 * 3600}})
        b = gate()
        assert b and "BROKER_DATA_STALE" in b["reasons"]
    finally:
        cleanup_attestation_user(email)
