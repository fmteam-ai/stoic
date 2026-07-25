"""Path B — connect an existing Forex VPS (works with any Windows VPS).

Enrollment codes, dashboard status ladder, MT5 discovery + import/clone
decisions with a single-writer guard, agent command queue, artifact
manifest (signed/checksummed), broker profile registry and the
failure-handling matrix (every automatic step has a recovery action).
"""
import hashlib
import logging
import os
import re
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("vps-pathb")

PATHB_LADDER = ["WAITING_FOR_AGENT", "AGENT_CONNECTED",
                "INSPECTING_SERVER", "READY_FOR_SETUP"]

ALLOWED_COMMANDS = {"install_mt5", "install_ea", "restart_terminal",
                    "rotate_logs", "freeze", "update_agent",
                    "run_diagnostics", "rollback_mt5", "rollback_agent"}
# failed command → compensation command (spec: failure handling)
COMPENSATION = {"install_mt5": "rollback_mt5",
                "update_agent": "rollback_agent",
                "install_ea": "restart_terminal"}

FAILURE_MATRIX = [
    {"failure": "provider_api_timeout",
     "response": "retry with the same idempotency key"},
    {"failure": "server_provisioning_fails",
     "response": "cancel or recreate"},
    {"failure": "agent_does_not_connect",
     "response": "show bootstrap diagnostics"},
    {"failure": "mt5_installer_fails",
     "response": "roll back installation"},
    {"failure": "ea_fails_to_heartbeat",
     "response": "restart terminal, then freeze"},
    {"failure": "broker_login_fails",
     "response": "request corrected credentials"},
    {"failure": "time_drift_detected",
     "response": "disable order entry"},
    {"failure": "disk_nearly_full",
     "response": "rotate logs and alert"},
    {"failure": "agent_update_fails",
     "response": "restore previous version"},
    {"failure": "vps_unreachable",
     "response": "freeze commands and start incident workflow"},
]

DISK_LOW_GB = 5
CLOCK_DRIFT_MS = 1000
UNREACHABLE_AFTER_SEC = 600


def _aware(dt):
    if dt and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _enrollment_code() -> str:
    letters = "".join(secrets.choice(string.ascii_uppercase)
                      for _ in range(3))
    digits = "".join(secrets.choice(string.digits) for _ in range(3))
    return f"{letters}-{digits}"


async def connect_existing(db, user_id: str, payload: dict) -> dict:
    """Step 1-2 — metadata form (never asks for the RDP password) →
    deployment + short enrollment code + safe install commands."""
    from vps_agent import BOOTSTRAP_TTL_MIN
    from vps_deployments import create_deployment
    dep = await create_deployment(db, user_id, {
        "path": "existing_vps", "provider": "existing",
        "region": payload.get("region"),
        "mt5_instances": int(payload.get("mt5_instances") or 1)}, None)
    now = datetime.now(timezone.utc)
    await db.vps_deployments.update_one(
        {"deployment_id": dep["deployment_id"]},
        {"$set": {"meta": {
            "provider_name": payload.get("provider_name"),
            "label": payload.get("label"),
            "os": payload.get("os") or "windows-server",
            "mt5_already_installed": bool(payload.get("mt5_installed"))},
            "retry_count": 0, "last_error_code": None}})
    code = _enrollment_code()
    token = f"bst_{secrets.token_urlsafe(32)}"
    await db.vps_bootstrap_tokens.insert_one({
        "token": token, "enrollment_code": code, "user_id": user_id,
        "deployment_id": dep["deployment_id"], "used": False,
        "created_at": now,
        "expires_at": now + timedelta(minutes=BOOTSTRAP_TTL_MIN)})
    base = os.environ.get("PUBLIC_BASE_URL") or "https://<your-stoic-host>"
    return {"deployment_id": dep["deployment_id"],
            "enrollment_code": code,
            "expires_in_min": BOOTSTRAP_TTL_MIN,
            "install_commands": {
                "recommended": [
                    f"Invoke-WebRequest -Uri \"{base}/api/infra/agent/"
                    f"bootstrap/installer?enrollment_code={code}\" "
                    f"-OutFile \"$env:TEMP\\stoic-agent.ps1\"",
                    "# Verify Authenticode signature / SHA-256 against "
                    "GET /api/infra/artifacts/manifest before running",
                    f"powershell.exe -ExecutionPolicy Bypass -File "
                    f"\"$env:TEMP\\stoic-agent.ps1\" "
                    f"-EnrollmentCode \"{code}\""]},
            "status": "WAITING_FOR_AGENT"}


async def resolve_enrollment(db, enrollment_code: str) -> str | None:
    doc = await db.vps_bootstrap_tokens.find_one(
        {"enrollment_code": enrollment_code.strip().upper(),
         "used": False})
    return doc["token"] if doc else None


async def pathb_status(db, user_id: str, deployment_id: str) -> dict:
    """Step 3 — the dashboard ladder, derived from agent events."""
    dep = await db.vps_deployments.find_one(
        {"user_id": user_id, "deployment_id": deployment_id})
    if not dep:
        raise ValueError("deployment not found")
    agent = await db.vps_agents.find_one(
        {"deployment_id": deployment_id, "revoked": {"$ne": True}})
    n_disc = await db.mt5_discovered.count_documents(
        {"deployment_id": deployment_id})
    if not agent:
        status = "WAITING_FOR_AGENT"
        diag = ("Agent has not connected yet. Diagnostics: confirm the "
                "enrollment code is unexpired (20 min), the VPS has "
                "outbound HTTPS, and PowerShell ran as Administrator.")
    elif n_disc == 0 and not agent.get("last_heartbeat"):
        status, diag = "AGENT_CONNECTED", None
    elif n_disc == 0:
        status, diag = "INSPECTING_SERVER", None
    else:
        status, diag = "READY_FOR_SETUP", None
    return {"deployment_id": deployment_id, "status": status,
            "ladder": PATHB_LADDER, "agent_id": (agent or {}).get("agent_id"),
            "discovered_terminals": n_disc, "diagnostics": diag,
            "meta": dep.get("meta") or {}}


# ── Step 4: MT5 discovery ───────────────────────────────────────
async def ingest_discovery(db, agent_token: str, terminals: list) -> dict:
    from vps_agent import agent_by_token
    agent = await agent_by_token(db, agent_token)
    now = datetime.now(timezone.utc)
    stored = 0
    for t in terminals[:50]:
        path = str(t.get("path") or "")[:300]
        if not path:
            continue
        await db.mt5_discovered.update_one(
            {"deployment_id": agent["deployment_id"], "path": path},
            {"$set": {"user_id": agent["user_id"],
                      "agent_id": agent["agent_id"],
                      "deployment_id": agent["deployment_id"],
                      "path": path,
                      "broker_hint": t.get("broker_hint"),
                      "account_login": (str(t.get("account_login"))
                                        if t.get("account_login") else None),
                      "running": bool(t.get("running")),
                      "ea_installed": bool(t.get("ea_installed")),
                      "source": t.get("source") or "scan",
                      "updated_at": now},
             "$setOnInsert": {"discovery_id": f"disc_{uuid.uuid4().hex[:10]}",
                              "decision": "pending", "created_at": now}},
            upsert=True)
        stored += 1
    return {"ok": True, "stored": stored}


async def _single_writer_conflict(db, user_id: str, account_login: str,
                                  exclude_discovery_id: str) -> str | None:
    """Never let two terminals trade the same account simultaneously."""
    if not account_login:
        return None
    inst = await db.mt5_instances.find_one(
        {"user_id": user_id, "account_ref": account_login,
         "managed": True, "status": {"$nin": ["frozen", "retired"]}})
    if inst:
        return (f"account {account_login} is already managed by instance "
                f"on deployment {inst.get('deployment_id')}")
    other = await db.mt5_discovered.find_one(
        {"user_id": user_id, "account_login": account_login,
         "decision": {"$in": ["manage", "clone"]},
         "discovery_id": {"$ne": exclude_discovery_id}})
    if other:
        return (f"account {account_login} is already claimed by terminal "
                f"{other.get('path')}")
    return None


async def decide_terminal(db, user_id: str, discovery_id: str,
                          action: str, consent: bool = False) -> dict:
    """Step 5 — manage / leave unmanaged / clone. Managing an existing
    production terminal requires explicit consent; cloning is the safe
    default. Single-writer guard blocks duplicate account control."""
    disc = await db.mt5_discovered.find_one(
        {"user_id": user_id, "discovery_id": discovery_id})
    if not disc:
        raise ValueError("discovered terminal not found")
    if action not in ("manage", "unmanage", "clone"):
        raise ValueError("action must be manage, unmanage or clone")
    now = datetime.now(timezone.utc)
    if action == "unmanage":
        await db.mt5_discovered.update_one(
            {"_id": disc["_id"]},
            {"$set": {"decision": "unmanage", "decided_at": now}})
        return {"ok": True, "decision": "unmanage"}
    if action == "manage" and not consent:
        raise PermissionError(
            "managing an existing production terminal modifies it — "
            "explicit consent required (clone is the safer option)")
    conflict = await _single_writer_conflict(
        db, user_id, disc.get("account_login"), discovery_id)
    if conflict:
        raise RuntimeError(f"single-writer guard: {conflict}")
    ref = disc.get("account_login") or f"clone-{uuid.uuid4().hex[:6]}"
    directory = (disc["path"] if action == "manage"
                 else f"C:\\STOIC\\MT5\\account-{ref}\\")
    await db.mt5_instances.update_one(
        {"deployment_id": disc["deployment_id"], "account_ref": ref},
        {"$set": {"user_id": user_id, "agent_id": disc.get("agent_id"),
                  "deployment_id": disc["deployment_id"],
                  "account_ref": ref, "directory": directory,
                  "broker": disc.get("broker_hint"), "managed": True,
                  "origin": action, "status": "adopted",
                  "updated_at": now},
         "$setOnInsert": {"created_at": now}}, upsert=True)
    await db.mt5_discovered.update_one(
        {"_id": disc["_id"]},
        {"$set": {"decision": action, "decided_at": now}})
    if action == "clone":
        await queue_command(db, user_id, disc.get("agent_id"),
                            "install_mt5",
                            {"clone_from": disc["path"],
                             "directory": directory}, "system:clone")
    return {"ok": True, "decision": action, "directory": directory}


# ── agent command queue ─────────────────────────────────────────
async def queue_command(db, user_id: str, agent_id: str, command: str,
                        params: dict | None, issued_by: str) -> dict:
    if command not in ALLOWED_COMMANDS:
        raise ValueError(f"unknown command '{command}'")
    # one account ↔ one terminal: EA installs must target exactly one
    # terminal — never blanket-deploy a token to every discovered MT5.
    if command == "install_ea":
        p = params or {}
        if not p.get("terminal_path") or not p.get("account_ref"):
            raise ValueError("install_ea requires explicit terminal_path "
                             "and account_ref — one terminal per account")
    agent = await db.vps_agents.find_one(
        {"agent_id": agent_id, "user_id": user_id,
         "revoked": {"$ne": True}})
    if not agent:
        raise ValueError("agent not found")
    if agent.get("commands_frozen") and command != "run_diagnostics":
        raise RuntimeError("commands are frozen for this agent "
                           "(incident workflow active)")
    cmd = {"command_id": f"cmd_{uuid.uuid4().hex[:10]}",
           "agent_id": agent_id, "user_id": user_id,
           "command": command, "params": params or {},
           "issued_by": issued_by, "status": "queued",
           "created_at": datetime.now(timezone.utc)}
    await db.agent_commands.insert_one(cmd)
    return {"command_id": cmd["command_id"], "status": "queued"}


async def poll_commands(db, agent_token: str) -> list:
    from vps_agent import agent_by_token
    agent = await agent_by_token(db, agent_token)
    now = datetime.now(timezone.utc)
    out = []
    async for c in db.agent_commands.find(
            {"agent_id": agent["agent_id"], "status": "queued"}
    ).sort("created_at", 1).limit(10):
        await db.agent_commands.update_one(
            {"_id": c["_id"]},
            {"$set": {"status": "delivered", "delivered_at": now}})
        out.append({"command_id": c["command_id"],
                    "command": c["command"], "params": c["params"]})
    return out


async def ack_command(db, agent_token: str, command_id: str,
                      ok: bool, detail: str = "") -> dict:
    """Failed command → compensation action queued automatically."""
    from vps_agent import agent_by_token
    agent = await agent_by_token(db, agent_token)
    cmd = await db.agent_commands.find_one(
        {"command_id": command_id, "agent_id": agent["agent_id"]})
    if not cmd:
        raise ValueError("command not found")
    now = datetime.now(timezone.utc)
    await db.agent_commands.update_one(
        {"_id": cmd["_id"]},
        {"$set": {"status": "acked" if ok else "failed",
                  "detail": detail[:500], "acked_at": now}})
    compensation = None
    if not ok and cmd["command"] in COMPENSATION:
        comp = COMPENSATION[cmd["command"]]
        res = await queue_command(db, agent["user_id"],
                                  agent["agent_id"], comp,
                                  {"compensates": command_id},
                                  "system:failure_matrix")
        compensation = {"command": comp, **res}
        try:
            from alerting import raise_alert
            await raise_alert(
                db, "vps_command_failed", "warning",
                f"Agent command {cmd['command']} failed on "
                f"{agent['agent_id']} — compensation '{comp}' queued. "
                f"{detail[:120]}",
                dedup_key=f"cmd_fail_{agent['agent_id']}_{cmd['command']}")
        except Exception as e:  # noqa: BLE001
            logger.warning("command-failure alert failed: %s", e)
    return {"ok": True, "compensation": compensation}


# ── health policies (failure matrix, live rules) ────────────────
async def apply_health_policies(db, agent: dict, metrics: dict) -> list:
    """Invoked on every agent heartbeat. Disk-low → rotate logs + alert;
    clock drift → disable order entry; recovery clears the flag."""
    actions = []
    now = datetime.now(timezone.utc)
    disk = metrics.get("disk_free_gb")
    if disk is not None and float(disk) < DISK_LOW_GB:
        pending = await db.agent_commands.find_one(
            {"agent_id": agent["agent_id"], "command": "rotate_logs",
             "status": {"$in": ["queued", "delivered"]}})
        if not pending:
            await queue_command(db, agent["user_id"], agent["agent_id"],
                                "rotate_logs", {"reason": "disk_low"},
                                "system:health_policy")
            actions.append("rotate_logs_queued")
        try:
            from alerting import raise_alert
            await raise_alert(db, "vps_disk_low", "warning",
                              f"VPS disk nearly full on "
                              f"{agent['agent_id']}: {disk} GB free",
                              dedup_key=f"disk_low_{agent['agent_id']}")
        except Exception:  # noqa: BLE001
            pass
    drift = metrics.get("clock_offset_ms")
    drifted = drift is not None and abs(float(drift)) > CLOCK_DRIFT_MS
    flags = agent.get("policy_flags") or {}
    if drifted != bool(flags.get("order_entry_disabled")):
        await db.vps_agents.update_one(
            {"_id": agent["_id"]},
            {"$set": {"policy_flags.order_entry_disabled": drifted,
                      "policy_flags.updated_at": now.isoformat()}})
        actions.append("order_entry_disabled"
                       if drifted else "order_entry_reenabled")
        if drifted:
            try:
                from alerting import raise_alert
                await raise_alert(
                    db, "vps_time_drift", "critical",
                    f"Time drift {drift}ms on {agent['agent_id']} — "
                    f"order entry disabled until clock resyncs",
                    dedup_key=f"drift_{agent['agent_id']}")
            except Exception:  # noqa: BLE001
                pass
    return actions


async def check_unreachable(db, user_id: str) -> list:
    """VPS becomes unreachable → freeze commands + incident workflow."""
    now = datetime.now(timezone.utc)
    frozen = []
    async for a in db.vps_agents.find({"user_id": user_id,
                                       "revoked": {"$ne": True},
                                       "commands_frozen": {"$ne": True}}):
        hb = _aware(a.get("last_heartbeat"))
        if hb and (now - hb).total_seconds() > UNREACHABLE_AFTER_SEC:
            await db.vps_agents.update_one(
                {"_id": a["_id"]},
                {"$set": {"commands_frozen": True,
                          "incident_opened_at": now}})
            await db.audit_log.insert_one({
                "user_id": user_id, "action": "vps_incident_opened",
                "detail": {"agent_id": a["agent_id"],
                           "reason": "unreachable — heartbeat "
                                     f"> {UNREACHABLE_AFTER_SEC}s"},
                "step_up_verified": False, "at": now})
            try:
                from alerting import raise_alert
                await raise_alert(
                    db, "vps_unreachable", "critical",
                    f"VPS agent {a['agent_id']} unreachable — commands "
                    f"frozen, incident workflow started",
                    dedup_key=f"unreachable_{a['agent_id']}")
            except Exception:  # noqa: BLE001
                pass
            frozen.append(a["agent_id"])
    return frozen


# ── artifact service ────────────────────────────────────────────
def _sha256_file(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def build_artifact_manifest() -> dict:
    ea_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "static", "EmergentTradingBridge.mq5")
    ea_version = None
    try:
        with open(ea_path) as f:
            m = re.search(r'EA_CLIENT_VERSION\s+"([\d.]+)"', f.read())
            ea_version = m.group(1) if m else None
    except OSError:
        pass
    return {"artifacts": [
        {"name": "stoic-agent", "type": "powershell", "version": "1.0.0",
         "url": "/api/infra/agent/bootstrap/installer",
         "sha256": None, "rollback_version": None,
         "note": "token-personalised script — verify TLS + enrollment "
                 "code binding; signed MSI planned"},
        {"name": "stoic-ea", "type": "mq5", "version": ea_version,
         "url": "/api/bot/ea/download", "sha256": _sha256_file(ea_path),
         "rollback_version": "1.53"},
        {"name": "stoic-ea-ex5", "type": "ex5", "version": ea_version,
         "url": None, "sha256": _sha256_file(ea_path.replace(".mq5",
                                                             ".ex5")),
         "rollback_version": "1.53",
         "note": "installers MUST deploy the exact CI-compiled, "
                 "hash-verified .ex5 — never recompile .mq5 locally. "
                 "Published by the signed release pipeline."},
    ], "generated_at": datetime.now(timezone.utc).isoformat()}


# ── broker profile registry ─────────────────────────────────────
async def broker_profiles(db) -> list:
    from vps_deployments import BROKER_CATALOG
    n = await db.broker_profiles.count_documents({})
    if n == 0:
        now = datetime.now(timezone.utc)
        await db.broker_profiles.insert_many([{
            "broker": b["broker"], "platform": "MT5",
            "server_names": [b["server"]],
            "installer_url": None, "installer_hash": None,
            "silent_arguments": "/auto",
            "expected_regions": list(b["region_estimates_ms"].keys())[:3],
            "known_restrictions": [],
            "certification_evidence": "see /api/broker-intel/qualification",
            "official_installer_required": True,
            "note": "download MetaTrader from the broker directly — a "
                    "generic installer may not connect correctly",
            "created_at": now} for b in BROKER_CATALOG
            if b["broker"] != "Other / custom"])
    return [{k: v for k, v in p.items() if k != "_id"}
            async for p in db.broker_profiles.find({})]


async def register_broker_installer(db, user_id: str, meta: dict) -> dict:
    """Unknown broker installer: hash + admin approval before deployment."""
    if not meta.get("broker") or not meta.get("sha256"):
        raise ValueError("broker and sha256 are required")
    now = datetime.now(timezone.utc)
    doc = {"installer_id": f"inst_{uuid.uuid4().hex[:10]}",
           "user_id": user_id, "broker": meta["broker"],
           "installer_url": meta.get("installer_url"),
           "sha256": meta["sha256"], "approved": False,
           "created_at": now}
    await db.broker_installers.insert_one(doc)
    return {"installer_id": doc["installer_id"], "approved": False,
            "note": "administrator approval required before deployment"}


async def approve_broker_installer(db, admin_user: dict,
                                   installer_id: str) -> dict:
    if admin_user.get("role") != "admin":
        raise PermissionError("admin role required")
    r = await db.broker_installers.update_one(
        {"installer_id": installer_id},
        {"$set": {"approved": True, "approved_by": admin_user["id"],
                  "approved_at": datetime.now(timezone.utc)}})
    if r.matched_count == 0:
        raise ValueError("installer not found")
    return {"ok": True, "installer_id": installer_id, "approved": True}
