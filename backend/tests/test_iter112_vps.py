"""iter-112 — VPS integration: provider abstraction, deployment state
machine + idempotency, bootstrap tokens, agent lifecycle, MT5 instances,
EA pairing, capacity/region recommendations."""
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

from vps_agent import (agent_heartbeat, claim_pairing_code,
                       consume_bootstrap_token, create_bootstrap_token,
                       create_pairing_code, register_agent,
                       register_mt5_instance, report_hardening)
from vps_deployments import (STATES, advance_deployment, create_deployment,
                             recommend_capacity)
from vps_providers import (PROVIDERS, PartnerRequiredError,
                           SimulatedProvider, VultrProvider, get_provider)


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter112-{uuid.uuid4().hex[:8]}"


# ─── providers ──────────────────────────────────────────────────
def test_provider_registry():
    assert set(PROVIDERS) == {"forexvps", "cns", "beeks", "vultr",
                              "simulated"}
    assert PROVIDERS["forexvps"]["method"] == "partner"
    assert isinstance(get_provider("simulated"), SimulatedProvider)
    assert isinstance(get_provider("vultr", "key"), VultrProvider)
    with pytest.raises(ValueError):
        get_provider("vultr")  # api key required
    with pytest.raises(ValueError):
        get_provider("nope")


def test_partner_providers_are_gated():
    p = get_provider("beeks")
    with pytest.raises(PartnerRequiredError):
        _run(p.list_regions())
    with pytest.raises(PartnerRequiredError):
        _run(p.create_server("london", "x", "y", "z"))


def test_simulated_provider_interface():
    p = get_provider("simulated")
    regions = _run(p.list_regions())
    plans = _run(p.list_plans())
    assert any(r["id"] == "london" for r in regions)
    assert any(pl["id"] == "4vcpu-8gb" for pl in plans)
    srv = _run(p.create_server("london", "2vcpu-4gb", "win", "t1"))
    assert srv["server_id"].startswith("sim-")
    assert _run(p.create_backup(srv["server_id"]))["ok"]


# ─── capacity rules (spec step 6) ───────────────────────────────
def test_capacity_rules():
    assert recommend_capacity(1)["plan"] == "2vcpu-4gb"
    assert recommend_capacity(2)["plan"] == "2vcpu-4gb"
    assert recommend_capacity(3)["plan"] == "4vcpu-8gb"
    assert recommend_capacity(5)["plan"] == "4vcpu-8gb"
    assert recommend_capacity(6)["plan"] == "8vcpu-16gb"
    assert recommend_capacity(10)["plan"] == "8vcpu-16gb"
    out = recommend_capacity(2, research_workload=True)
    assert any("SEPARATE" in n for n in out["notes"])
    assert recommend_capacity(1, local_ai=True)["vcpu"] >= 4


# ─── deployments + idempotency ──────────────────────────────────
def test_deployment_idempotency(db):
    async def go():
        key = f"idem-{uuid.uuid4().hex[:6]}"
        d1 = await create_deployment(db, UID, {"provider": "simulated",
                                               "region": "london"}, key)
        d2 = await create_deployment(db, UID, {"provider": "simulated",
                                               "region": "london"}, key)
        assert d1["deployment_id"] == d2["deployment_id"]
        n = await db.vps_deployments.count_documents(
            {"user_id": UID, "idempotency_key": key})
        assert n == 1
    _run(go())


def test_deployment_never_starts_live(db):
    async def go():
        d = await create_deployment(db, UID, {"provider": "simulated",
                                              "mode": "autonomous_live"},
                                    None)
        assert d["mode"] == "shadow"
        assert d["state"] == "REQUESTED"
        assert d["state"] in STATES
    _run(go())


def test_simulated_state_machine_advances(db):
    async def go():
        d = await create_deployment(db, UID, {"provider": "simulated",
                                              "region": "london",
                                              "plan": "2vcpu-4gb"}, None)
        # backdate creation so the timer has "elapsed"
        await db.vps_deployments.update_one(
            {"deployment_id": d["deployment_id"]},
            {"$set": {"created_at": datetime.now(timezone.utc)
                      - timedelta(seconds=60)}})
        out = await advance_deployment(db, UID, d["deployment_id"])
        assert out["state"] == "READY"
        states = [s["state"] for s in out["state_history"]]
        assert states[0] == "REQUESTED" and states[-1] == "READY"
        assert out["server"]["server_id"].startswith("sim-")
    _run(go())


def test_existing_vps_requires_agent_before_ready(db):
    async def go():
        d = await create_deployment(db, UID, {"provider": "existing",
                                              "path": "existing_vps",
                                              "mt5_instances": 1}, None)
        await db.vps_deployments.update_one(
            {"deployment_id": d["deployment_id"]},
            {"$set": {"created_at": datetime.now(timezone.utc)
                      - timedelta(seconds=120)}})
        out = await advance_deployment(db, UID, d["deployment_id"])
        # no agent yet — an elapsed timer alone must NOT mean READY
        assert out["state"] == "REQUESTED"
    _run(go())


# ─── bootstrap tokens (spec step 9) ─────────────────────────────
def test_bootstrap_token_single_use(db):
    async def go():
        t = await create_bootstrap_token(db, UID, "dep_test1")
        doc = await consume_bootstrap_token(db, t["token"])
        assert doc["deployment_id"] == "dep_test1"
        with pytest.raises(ValueError, match="already used"):
            await consume_bootstrap_token(db, t["token"])
    _run(go())


def test_bootstrap_token_expiry(db):
    async def go():
        t = await create_bootstrap_token(db, UID, "dep_test2")
        await db.vps_bootstrap_tokens.update_one(
            {"token": t["token"]},
            {"$set": {"expires_at": datetime.now(timezone.utc)
                      - timedelta(minutes=1)}})
        with pytest.raises(ValueError, match="expired"):
            await consume_bootstrap_token(db, t["token"])
        with pytest.raises(ValueError, match="invalid"):
            await consume_bootstrap_token(db, "bst_nope")
    _run(go())


# ─── agent lifecycle (steps 11-20) ──────────────────────────────
def test_agent_full_lifecycle_drives_deployment(db):
    async def go():
        d = await create_deployment(db, UID, {"provider": "existing",
                                              "path": "existing_vps",
                                              "mt5_instances": 1}, None)
        t = await create_bootstrap_token(db, UID, d["deployment_id"])
        reg = await register_agent(db, t["token"], {
            "machine_fingerprint": "fp-123", "agent_version": "1.0.0",
            "windows_version": "Server 2022", "cpu": "test",
            "ram_gb": 8, "disk_free_gb": 70, "public_ip": "203.0.113.9",
            "timezone": "UTC", "clock_offset_ms": 14})
        assert reg["agent_id"].startswith("agt_")
        assert "heartbeat" in reg["capabilities"]
        # bootstrap token is revoked by registration
        with pytest.raises(ValueError):
            await consume_bootstrap_token(db, t["token"])

        out = await advance_deployment(db, UID, d["deployment_id"])
        assert out["state"] == "AGENT_CONNECTED"

        hb = await agent_heartbeat(db, reg["agent_token"], {
            "cpu_percent": 18, "ram_percent": 41, "disk_free_gb": 72,
            "clock_offset_ms": 14, "mt5_processes": 1,
            "agent_version": "1.0.0"})
        assert hb["ok"]

        hard = await report_hardening(db, reg["agent_token"], {
            "firewall_enabled": True, "time_sync_configured": True})
        assert hard["complete"] is False
        assert "rdp_restricted" in hard["missing"]

        inst = await register_mt5_instance(db, reg["agent_token"], {
            "account_ref": "10001", "broker": "TestBroker",
            "ea_version": "1.54", "status": "installed"})
        assert "account-10001" in inst["directory"]

        out = await advance_deployment(db, UID, d["deployment_id"])
        assert out["state"] == "READY"
    _run(go())


def test_agent_token_auth_rejects_unknown(db):
    async def go():
        with pytest.raises(ValueError):
            await agent_heartbeat(db, "agt_tok_bogus", {})
    _run(go())


# ─── EA pairing (step 19, hardened in iter-114) ─────────────────
def test_pairing_code_flow(db):
    async def go():
        res = await db.accounts.insert_one({
            "user_id": UID, "mode": "demo", "label": "pair-test",
            "bridge_token": f"tok-{uuid.uuid4().hex}"})
        p = await create_pairing_code(db, UID, str(res.inserted_id))
        assert p["code"].startswith("PAIR-")
        claim = await claim_pairing_code(db, p["code"], {
            "terminal_path": "C:\\STOIC\\MT5\\account-1\\",
            "host_fingerprint": "host-a"})
        assert claim["bridge_token"].startswith("tok_")
        assert claim["account_id"] == str(res.inserted_id)
        assert claim["connected"] is False
        with pytest.raises(ValueError, match="already-claimed|invalid"):
            await claim_pairing_code(db, p["code"], {
                "terminal_path": "x", "host_fingerprint": "y"})
    _run(go())


def test_pairing_rejects_foreign_account(db):
    async def go():
        res = await db.accounts.insert_one({
            "user_id": f"{UID}-other", "label": "foreign",
            "bridge_token": f"tok-{uuid.uuid4().hex}"})
        with pytest.raises(ValueError, match="not found"):
            await create_pairing_code(db, UID, str(res.inserted_id))
    _run(go())


# ─── Cleanup ────────────────────────────────────────────────────
def test_zz_cleanup(db):
    async def go():
        rx = {"$regex": f"^{UID}"}
        for coll in ("vps_deployments", "vps_bootstrap_tokens",
                     "vps_agents", "mt5_instances", "ea_pairing_codes",
                     "accounts", "vps_provider_creds", "vps_backups",
                     "audit_log"):
            await db[coll].delete_many({"user_id": rx})
    _run(go())
