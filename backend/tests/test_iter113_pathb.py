"""iter-113 — Path B: enrollment codes, status ladder, MT5 discovery +
decisions (single-writer guard), agent command queue with compensation,
health policies, artifact manifest, broker profiles, failure matrix."""
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

from vps_agent import register_agent
from vps_pathb import (ALLOWED_COMMANDS, COMPENSATION, FAILURE_MATRIX,
                       ack_command, apply_health_policies,
                       approve_broker_installer, broker_profiles,
                       build_artifact_manifest, check_unreachable,
                       connect_existing, decide_terminal, ingest_discovery,
                       pathb_status, poll_commands, queue_command,
                       register_broker_installer, resolve_enrollment)


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter113-{uuid.uuid4().hex[:8]}"
CTX = {}


# ─── connect + enrollment + ladder ──────────────────────────────
def test_connect_existing_and_enrollment(db):
    async def go():
        out = await connect_existing(db, UID, {
            "provider_name": "ForexVPS", "label": "my-vps",
            "region": "London", "os": "windows-server",
            "mt5_installed": True})
        assert out["status"] == "WAITING_FOR_AGENT"
        code = out["enrollment_code"]
        assert len(code) == 7 and code[3] == "-"
        assert "-EnrollmentCode" in out["install_commands"]["recommended"][-1]
        assert "Verify" in out["install_commands"]["recommended"][1]
        token = await resolve_enrollment(db, code.lower())
        assert token and token.startswith("bst_")
        CTX["dep"] = out["deployment_id"]
        CTX["code"] = code
    _run(go())


def test_ladder_advances_with_agent_events(db):
    async def go():
        dep = CTX["dep"]
        s = await pathb_status(db, UID, dep)
        assert s["status"] == "WAITING_FOR_AGENT"
        assert s["diagnostics"]  # bootstrap diagnostics shown
        token = await resolve_enrollment(db, CTX["code"])
        reg = await register_agent(db, token, {
            "machine_fingerprint": "fp-b", "agent_version": "1.0.0"})
        CTX["agent_id"], CTX["agent_token"] = reg["agent_id"], \
            reg["agent_token"]
        s = await pathb_status(db, UID, dep)
        assert s["status"] == "AGENT_CONNECTED"
        # first heartbeat → inspecting
        from vps_agent import agent_heartbeat
        await agent_heartbeat(db, reg["agent_token"], {"cpu_percent": 5})
        s = await pathb_status(db, UID, dep)
        assert s["status"] == "INSPECTING_SERVER"
    _run(go())


# ─── discovery + decisions ──────────────────────────────────────
def test_discovery_ingest_and_ready(db):
    async def go():
        out = await ingest_discovery(db, CTX["agent_token"], [
            {"path": "C:\\Program Files\\IC Markets MT5",
             "broker_hint": "IC Markets", "account_login": "123456",
             "running": True, "ea_installed": False,
             "source": "filesystem"},
            {"path": "C:\\Program Files\\Pepperstone MT5",
             "broker_hint": "Pepperstone", "running": False,
             "ea_installed": False, "source": "registry"}])
        assert out["stored"] == 2
        s = await pathb_status(db, UID, CTX["dep"])
        assert s["status"] == "READY_FOR_SETUP"
        docs = [d async for d in db.mt5_discovered.find(
            {"deployment_id": CTX["dep"]})]
        CTX["disc_ic"] = next(d["discovery_id"] for d in docs
                              if d["broker_hint"] == "IC Markets")
        CTX["disc_pep"] = next(d["discovery_id"] for d in docs
                               if d["broker_hint"] == "Pepperstone")
    _run(go())


def test_manage_requires_explicit_consent(db):
    async def go():
        with pytest.raises(PermissionError, match="consent"):
            await decide_terminal(db, UID, CTX["disc_ic"], "manage",
                                  consent=False)
    _run(go())


def test_clone_creates_isolated_instance(db):
    async def go():
        out = await decide_terminal(db, UID, CTX["disc_ic"], "clone")
        assert out["decision"] == "clone"
        assert out["directory"] == "C:\\STOIC\\MT5\\account-123456\\"
        inst = await db.mt5_instances.find_one(
            {"user_id": UID, "account_ref": "123456"})
        assert inst["origin"] == "clone" and inst["managed"] is True
        # clone queues an install_mt5 command for the agent
        cmd = await db.agent_commands.find_one(
            {"agent_id": CTX["agent_id"], "command": "install_mt5"})
        assert cmd and cmd["params"]["clone_from"].startswith("C:\\Program")
    _run(go())


def test_single_writer_guard_blocks_duplicate_account(db):
    async def go():
        # second terminal claiming the SAME account 123456
        await ingest_discovery(db, CTX["agent_token"], [
            {"path": "D:\\Another\\MT5", "broker_hint": "IC Markets",
             "account_login": "123456", "running": False}])
        dup = await db.mt5_discovered.find_one(
            {"deployment_id": CTX["dep"], "path": "D:\\Another\\MT5"})
        with pytest.raises(RuntimeError, match="single-writer"):
            await decide_terminal(db, UID, dup["discovery_id"], "manage",
                                  consent=True)
    _run(go())


def test_unmanage_decision(db):
    async def go():
        out = await decide_terminal(db, UID, CTX["disc_pep"], "unmanage")
        assert out["decision"] == "unmanage"
        d = await db.mt5_discovered.find_one(
            {"discovery_id": CTX["disc_pep"]})
        assert d["decision"] == "unmanage"
    _run(go())


# ─── command queue + compensation ───────────────────────────────
def test_command_queue_lifecycle(db):
    async def go():
        q = await queue_command(db, UID, CTX["agent_id"],
                                "restart_terminal", {"account": "123456"},
                                f"user:{UID}")
        assert q["status"] == "queued"
        cmds = await poll_commands(db, CTX["agent_token"])
        ids = [c["command_id"] for c in cmds]
        assert q["command_id"] in ids
        # second poll returns nothing new (delivered)
        again = await poll_commands(db, CTX["agent_token"])
        assert q["command_id"] not in [c["command_id"] for c in again]
        ack = await ack_command(db, CTX["agent_token"], q["command_id"],
                                ok=True, detail="restarted")
        assert ack["compensation"] is None
    _run(go())


def test_failed_command_triggers_compensation(db):
    async def go():
        q = await queue_command(db, UID, CTX["agent_id"], "install_mt5",
                                {}, f"user:{UID}")
        await poll_commands(db, CTX["agent_token"])
        ack = await ack_command(db, CTX["agent_token"], q["command_id"],
                                ok=False, detail="installer hash mismatch")
        assert ack["compensation"]["command"] == "rollback_mt5"
        comp = await db.agent_commands.find_one(
            {"agent_id": CTX["agent_id"], "command": "rollback_mt5"})
        assert comp["params"]["compensates"] == q["command_id"]
    _run(go())


def test_unknown_command_rejected(db):
    async def go():
        with pytest.raises(ValueError, match="unknown command"):
            await queue_command(db, UID, CTX["agent_id"], "format_c",
                                {}, "user:x")
        assert COMPENSATION["update_agent"] == "rollback_agent"
        assert "freeze" in ALLOWED_COMMANDS
    _run(go())


# ─── health policies (failure matrix live rules) ────────────────
def test_disk_low_rotates_logs_and_alerts(db):
    async def go():
        agent = await db.vps_agents.find_one({"agent_id": CTX["agent_id"]})
        actions = await apply_health_policies(db, agent,
                                              {"disk_free_gb": 2.5})
        assert "rotate_logs_queued" in actions
        # dedup — second call does not re-queue
        actions2 = await apply_health_policies(db, agent,
                                               {"disk_free_gb": 2.5})
        assert "rotate_logs_queued" not in actions2
    _run(go())


def test_time_drift_disables_order_entry(db):
    async def go():
        agent = await db.vps_agents.find_one({"agent_id": CTX["agent_id"]})
        actions = await apply_health_policies(db, agent,
                                              {"clock_offset_ms": 5000})
        assert "order_entry_disabled" in actions
        agent = await db.vps_agents.find_one({"agent_id": CTX["agent_id"]})
        assert agent["policy_flags"]["order_entry_disabled"] is True
        # clock recovers → flag clears
        actions = await apply_health_policies(db, agent,
                                              {"clock_offset_ms": 20})
        assert "order_entry_reenabled" in actions
    _run(go())


def test_unreachable_freezes_commands_and_opens_incident(db):
    async def go():
        await db.vps_agents.update_one(
            {"agent_id": CTX["agent_id"]},
            {"$set": {"last_heartbeat": datetime.now(timezone.utc)
                      - timedelta(minutes=30)}})
        frozen = await check_unreachable(db, UID)
        assert CTX["agent_id"] in frozen
        with pytest.raises(RuntimeError, match="frozen"):
            await queue_command(db, UID, CTX["agent_id"],
                                "restart_terminal", {}, "user:x")
        # diagnostics still allowed
        q = await queue_command(db, UID, CTX["agent_id"],
                                "run_diagnostics", {}, "user:x")
        assert q["status"] == "queued"
        assert await db.audit_log.find_one(
            {"user_id": UID, "action": "vps_incident_opened"})
    _run(go())


# ─── artifacts + broker profiles + matrix ───────────────────────
def test_artifact_manifest_has_real_ea_checksum(db):
    m = build_artifact_manifest()
    ea = next(a for a in m["artifacts"] if a["name"] == "stoic-ea")
    assert ea["version"] == "1.54"
    assert ea["sha256"] and len(ea["sha256"]) == 64
    assert ea["rollback_version"] == "1.53"


def test_broker_profiles_seeded(db):
    async def go():
        profiles = await broker_profiles(db)
        assert len(profiles) >= 3
        p = profiles[0]
        for k in ("broker", "server_names", "silent_arguments",
                  "official_installer_required", "note"):
            assert k in p
    _run(go())


def test_custom_installer_needs_admin_approval(db):
    async def go():
        reg = await register_broker_installer(db, UID, {
            "broker": "ObscureBroker", "sha256": "a" * 64,
            "installer_url": "https://broker.example/mt5.exe"})
        assert reg["approved"] is False
        with pytest.raises(PermissionError):
            await approve_broker_installer(db, {"id": UID, "role": "user"},
                                           reg["installer_id"])
        ok = await approve_broker_installer(
            db, {"id": "admin", "role": "admin"}, reg["installer_id"])
        assert ok["approved"] is True
    _run(go())


def test_failure_matrix_complete():
    failures = {r["failure"] for r in FAILURE_MATRIX}
    assert failures == {
        "provider_api_timeout", "server_provisioning_fails",
        "agent_does_not_connect", "mt5_installer_fails",
        "ea_fails_to_heartbeat", "broker_login_fails",
        "time_drift_detected", "disk_nearly_full", "agent_update_fails",
        "vps_unreachable"}
    assert all(r["response"] for r in FAILURE_MATRIX)


# ─── Cleanup ────────────────────────────────────────────────────
def test_zz_cleanup(db):
    async def go():
        rx = {"$regex": f"^{UID}"}
        for coll in ("vps_deployments", "vps_bootstrap_tokens",
                     "vps_agents", "mt5_instances", "mt5_discovered",
                     "agent_commands", "broker_installers", "audit_log"):
            await db[coll].delete_many({"user_id": rx})
        await db.ops_alerts.delete_many(
            {"dedup_key": {"$regex": CTX.get("agent_id", "none")}})
    _run(go())
