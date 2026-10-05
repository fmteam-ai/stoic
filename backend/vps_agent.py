"""STOIC Agent lifecycle — bootstrap tokens, registration, scoped agent
tokens (mTLS later), heartbeats, hardening report, MT5 instance registry
and EA pairing codes (infra spec steps 9-20)."""
import secrets
import string
import uuid
import os
import hashlib
from datetime import datetime, timedelta, timezone

from identity_model import (authoritative_account_number,
                            expected_broker_server)

BOOTSTRAP_TTL_MIN = 20
PAIRING_TTL_MIN = 10


def hash_agent_token(token: str) -> str:
    """iter-170 — agent tokens are stored HASHED at rest so DB read access
    alone can't yield live credentials. Tokens are 256-bit random
    (secrets.token_urlsafe(32)), so a plain SHA-256 is unbrute-forceable —
    no salt/bcrypt needed (same model as API-key/PAT storage)."""
    return hashlib.sha256((token or "").encode()).hexdigest()

HARDENING_CHECKLIST = [
    "firewall_enabled", "inbound_restricted", "unnecessary_services_off",
    "defender_configured", "time_sync_configured", "service_account_nonadmin",
    "log_retention_set", "smb_disabled", "rdp_restricted",
    "admin_password_rotated", "runtimes_minimal"]


def _aware(dt):
    if dt and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def create_bootstrap_token(db, user_id: str,
                                 deployment_id: str) -> dict:
    """Single-use, 20-min token tied to a deployment (spec step 9)."""
    token = f"bst_{secrets.token_urlsafe(32)}"
    now = datetime.now(timezone.utc)
    await db.vps_bootstrap_tokens.insert_one({
        "token": token, "user_id": user_id,
        "deployment_id": deployment_id, "used": False,
        "created_at": now,
        "expires_at": now + timedelta(minutes=BOOTSTRAP_TTL_MIN)})
    return {"token": token, "expires_in_min": BOOTSTRAP_TTL_MIN,
            "deployment_id": deployment_id}


async def consume_bootstrap_token(db, token: str) -> dict:
    """Verify + burn. Raises ValueError on invalid/expired/reused."""
    doc = await db.vps_bootstrap_tokens.find_one({"token": token})
    if not doc:
        raise ValueError("invalid bootstrap token")
    if doc.get("used"):
        raise ValueError("bootstrap token already used")
    if _aware(doc["expires_at"]) < datetime.now(timezone.utc):
        raise ValueError("bootstrap token expired")
    r = await db.vps_bootstrap_tokens.update_one(
        {"_id": doc["_id"], "used": False},
        {"$set": {"used": True,
                  "used_at": datetime.now(timezone.utc)}})
    if r.modified_count != 1:  # lost the race — someone burned it first
        raise ValueError("bootstrap token already used")
    return doc


def encrypt_command_key(plain: str) -> dict:
    """iter-176 — HMAC command keys are encrypted at rest (secrets_vault)."""
    import secrets_vault
    return secrets_vault.encrypt(plain, associated_data=b"agent_command_key")


def agent_command_key(agent: dict) -> str | None:
    """Decrypt the agent's command-signing key. Legacy plaintext fallback
    keeps pre-migration agents working until the startup migration runs."""
    if (agent or {}).get("command_key_enc"):
        import secrets_vault
        return secrets_vault.decrypt(agent["command_key_enc"],
                                     associated_data=b"agent_command_key")
    return (agent or {}).get("command_key")


async def register_agent(db, token: str, facts: dict) -> dict:
    """Bootstrap token → unique agent id + scoped revocable agent token
    (fingerprint-bound). The bootstrap token is revoked here (step 11)."""
    boot = await consume_bootstrap_token(db, token)
    now = datetime.now(timezone.utc)
    # SEC (3rd audit) — per-tenant agent cap: an unbounded agent count would
    # let one customer manufacture fake corroboration / exhaust resources.
    max_agents = int(os.environ.get("VPS_MAX_AGENTS_PER_USER", "25"))
    active = await db.vps_agents.count_documents(
        {"user_id": boot["user_id"], "revoked": {"$ne": True}})
    if active >= max_agents:
        raise ValueError(
            f"agent limit reached for this account ({max_agents}); revoke an "
            "existing agent before registering another")
    agent_id = f"agt_{uuid.uuid4().hex[:12]}"
    agent_token = f"agt_tok_{secrets.token_urlsafe(32)}"
    command_key = secrets.token_hex(32)  # iter-122 P3: HMAC key for signed commands
    await db.vps_agents.insert_one({
        "agent_id": agent_id, "agent_token_hash": hash_agent_token(agent_token),
        "command_key_enc": encrypt_command_key(command_key),  # iter-176: encrypted at rest
        "command_seq": 0, "last_acked_seq": 0,
        "user_id": boot["user_id"],
        "deployment_id": boot["deployment_id"],
        "fingerprint": facts.get("machine_fingerprint"),
        "facts": {k: facts.get(k) for k in (
            "agent_version", "windows_version", "cpu", "ram_gb",
            "disk_free_gb", "public_ip", "timezone", "clock_offset_ms")},
        "hardening": {}, "revoked": False,
        "registered_at": now, "last_heartbeat": None})
    return {"agent_id": agent_id, "agent_token": agent_token,
            "command_key": command_key,
            "heartbeat_interval_sec": 60,
            "capabilities": ["heartbeat", "hardening", "mt5_install",
                             "ea_install", "health_check"],
            "note": ("installation-scoped credentials — agent_token for "
                     "auth, command_key verifies signed command sequence; "
                     "mTLS enrollment requires PKI at deploy time")}


async def agent_by_token(db, agent_token: str) -> dict:
    # Primary lookup is by token HASH (plaintext is never stored). A legacy
    # plaintext fallback keeps any not-yet-migrated agent authenticating
    # during rollout; the startup migration removes all plaintext in prod.
    agent = await db.vps_agents.find_one(
        {"agent_token_hash": hash_agent_token(agent_token),
         "revoked": {"$ne": True}})
    if not agent:
        agent = await db.vps_agents.find_one(
            {"agent_token": agent_token, "revoked": {"$ne": True}})
    if not agent:
        raise ValueError("unknown or revoked agent token")
    return agent


async def agent_heartbeat(db, agent_token: str, metrics: dict) -> dict:
    """Infrastructure truth — kept strictly separate from EA/trading
    heartbeats (spec step 20)."""
    agent = await agent_by_token(db, agent_token)
    now = datetime.now(timezone.utc)
    hb = {k: metrics.get(k) for k in (
        "cpu_percent", "ram_percent", "disk_free_gb", "clock_offset_ms",
        "mt5_processes", "agent_version",
        # iter-139 host-agent telemetry expansion
        "disk_free_pct", "broker_latency_ms", "mt5_connected",
        "ea_attached", "restarts_24h", "service_uptime_sec")}
    await db.vps_agents.update_one(
        {"_id": agent["_id"]},
        {"$set": {"last_heartbeat": now, "last_metrics": hb}})
    out = {"ok": True, "next_heartbeat_sec": 60}
    # iter-160 — central config sync: ship desired config with the ack
    if agent.get("desired_config"):
        out["desired_config"] = agent["desired_config"]
    return out


async def rotate_agent_token(db, agent_token: str) -> dict:
    """iter-157 — agent-initiated credential renewal. The agent presents its
    CURRENT valid token and receives a fresh one; the old token dies
    immediately. Long-lived static credentials never accumulate."""
    agent = await agent_by_token(db, agent_token)
    if not agent:
        raise ValueError("unknown or revoked agent token")
    new_token = f"agt_tok_{secrets.token_urlsafe(32)}"
    now = datetime.now(timezone.utc).isoformat()
    await db.vps_agents.update_one(
        {"agent_id": agent["agent_id"]},
        {"$set": {"agent_token_hash": hash_agent_token(new_token),
                  "token_rotated_at": now},
         "$unset": {"agent_token": ""}})
    return {"agent_id": agent["agent_id"], "agent_token": new_token,
            "rotated_at": now,
            "note": "old token revoked immediately — persist the new one "
                    "before your next heartbeat"}


async def report_hardening(db, agent_token: str, checklist: dict) -> dict:
    agent = await agent_by_token(db, agent_token)
    status = {k: bool(checklist.get(k)) for k in HARDENING_CHECKLIST}
    await db.vps_agents.update_one(
        {"_id": agent["_id"]},
        {"$set": {"hardening": status,
                  "hardening_at": datetime.now(timezone.utc)}})
    missing = [k for k, v in status.items() if not v]
    return {"ok": True, "complete": not missing, "missing": missing}


async def register_mt5_instance(db, agent_token: str, payload: dict) -> dict:
    """One MT5 instance per trading account, isolated directory
    (spec step 14)."""
    agent = await agent_by_token(db, agent_token)
    account_ref = str(payload.get("account_ref") or "")
    if not account_ref:
        raise ValueError("account_ref is required")
    directory = payload.get("directory") \
        or f"C:\\STOIC\\MT5\\account-{account_ref}\\"
    now = datetime.now(timezone.utc)
    await db.mt5_instances.update_one(
        {"deployment_id": agent["deployment_id"],
         "account_ref": account_ref},
        {"$set": {"user_id": agent["user_id"],
                  "agent_id": agent["agent_id"],
                  "deployment_id": agent["deployment_id"],
                  "account_ref": account_ref, "directory": directory,
                  "broker": payload.get("broker"),
                  "mt5_build": payload.get("mt5_build"),
                  "ea_version": payload.get("ea_version"),
                  "status": payload.get("status") or "installed",
                  "updated_at": now},
         "$setOnInsert": {"created_at": now}},
        upsert=True)
    return {"ok": True, "directory": directory}


def _pairing_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    part = lambda: "".join(secrets.choice(alphabet) for _ in range(4))  # noqa: E731
    return f"PAIR-{part()}-{part()}"


def _digest(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode()).hexdigest()


LEASE_SECONDS = 20

DEPLOY_STATES = ["TOKEN_ISSUED", "TOKEN_CLAIMED", "HOST_INSPECTED",
                 "TERMINAL_SELECTED", "ARTIFACT_VERIFIED", "EA_INSTALLED",
                 "EA_HEARTBEAT_RECEIVED", "BROKER_ACCOUNT_VERIFIED",
                 "READY_FOR_SHADOW", "FAILED"]
AGENT_PROGRESS_STATES = {"HOST_INSPECTED", "ARTIFACT_VERIFIED",
                         "EA_INSTALLED", "FAILED"}


async def advance_ea_deployment(db, account_id: str, state: str,
                                detail: str = "") -> dict | None:
    """Forward-only deployment state machine — 'paired' never implies
    'deployed'; READY_FOR_SHADOW requires a verified EA heartbeat."""
    if state not in DEPLOY_STATES:
        raise ValueError(f"unknown deployment state '{state}'")
    dep = await db.ea_deployments.find_one(
        {"account_id": account_id, "state": {"$ne": "FAILED"}},
        sort=[("created_at", -1)])
    if not dep:
        return None
    now = datetime.now(timezone.utc)
    if state != "FAILED" and DEPLOY_STATES.index(state) <= \
            DEPLOY_STATES.index(dep["state"]):
        return dep
    await db.ea_deployments.update_one(
        {"_id": dep["_id"]},
        {"$set": {"state": state, "updated_at": now},
         "$push": {"state_history": {"state": state, "detail": detail,
                                     "at": now.isoformat()}}})
    return await db.ea_deployments.find_one({"_id": dep["_id"]})


async def create_pairing_code(db, user_id: str, account_id: str,
                              expected_login: str | None = None,
                              expected_server: str | None = None,
                              revoke_existing: bool = False) -> dict:
    """One-time EA pairing code — digest-only storage, 10-min TTL, scoped
    to ONE account. Blocked while another installation holds the
    execution-owner lease unless the caller explicitly revokes it."""
    from bson import ObjectId
    acc = await db.accounts.find_one(
        {"_id": ObjectId(account_id), "user_id": user_id})
    if not acc:
        raise ValueError("account not found")
    now = datetime.now(timezone.utc)
    lease = await db.execution_leases.find_one({"account_id": account_id})
    active = (lease and not lease.get("revoked")
              and _aware(lease["expires_at"]) > now)
    if active and not revoke_existing:
        raise RuntimeError(
            f"account already has an active execution owner "
            f"({lease['installation_id']}, lease {LEASE_SECONDS}s) — "
            f"revoke it explicitly (revoke_existing) to re-pair")
    if lease and revoke_existing:
        await db.execution_leases.update_one(
            {"_id": lease["_id"]},
            {"$set": {"revoked": True, "revoked_at": now}})
        await db.installations.update_many(
            {"account_id": account_id, "revoked": {"$ne": True}},
            {"$set": {"revoked": True, "revoked_at": now,
                      "revoked_reason": "re-pair"}})
    code = _pairing_code()
    await db.ea_pairing_codes.insert_one({
        "code_digest": _digest(code), "user_id": user_id,
        "account_id": account_id, "consumed_at": None,
        "created_at": now,
        "expires_at": now + timedelta(minutes=PAIRING_TTL_MIN)})
    dep_id = f"eadep_{uuid.uuid4().hex[:10]}"
    await db.ea_deployments.insert_one({
        "ea_deployment_id": dep_id, "user_id": user_id,
        "account_id": account_id,
        "expected_login": expected_login,
        "expected_server": expected_server or acc.get("server"),
        "state": "TOKEN_ISSUED",
        "state_history": [{"state": "TOKEN_ISSUED", "detail": "",
                           "at": now.isoformat()}],
        "created_at": now, "updated_at": now})
    return {"code": code, "expires_in_min": PAIRING_TTL_MIN,
            "account_id": account_id, "ea_deployment_id": dep_id,
            "note": "code shown once — only a digest is stored"}


async def claim_pairing_code(db, code: str, terminal: dict) -> dict:
    """Atomic single-claimant exchange. Requires binding to EXACTLY one
    MT5 terminal on one host (one account → one installation identity →
    one terminal → one Windows host). Rotates the account bridge token,
    instantly cutting off any previous terminal."""
    terminal = terminal or {}
    terminal_path = str(terminal.get("terminal_path") or "").strip()
    host = str(terminal.get("host_fingerprint") or "").strip()
    if not terminal_path or not host:
        raise ValueError(
            "exactly one MT5 terminal must be selected — terminal_path "
            "and host_fingerprint are required (never deploy one token "
            "to every terminal)")
    from bson import ObjectId
    now = datetime.now(timezone.utc)
    # atomic: digest match + not consumed + unexpired — one winner only
    doc = await db.ea_pairing_codes.find_one_and_update(
        {"code_digest": _digest(str(code or "").strip().upper()),
         "consumed_at": None, "expires_at": {"$gt": now}},
        {"$set": {"consumed_at": now,
                  "claimed_terminal": terminal_path,
                  "claimed_host": host}})
    if not doc:
        raise ValueError("invalid, expired or already-claimed pairing code")
    acc = await db.accounts.find_one({"_id": ObjectId(doc["account_id"])})
    if not acc:
        raise ValueError("account no longer exists")
    # rotate bridge credential — strict single-writer
    new_token = f"tok_{secrets.token_urlsafe(32)}"
    # P1-01 — the agent receives the plaintext once (below); storage keeps only the keyed hash
    await db.accounts.update_one(
        {"_id": acc["_id"]},
        __import__("bridge_tokens").rotation_update(acc, new_token, grace_until=None, suspended=True))
    installation_id = f"inst_{uuid.uuid4().hex[:12]}"
    await db.installations.insert_one({
        "installation_id": installation_id,
        "user_id": doc["user_id"], "account_id": doc["account_id"],
        "terminal_path": terminal_path, "host_fingerprint": host,
        "revoked": False, "created_at": now})
    await db.execution_leases.update_one(
        {"account_id": doc["account_id"]},
        {"$set": {"installation_id": installation_id,
                  "user_id": doc["user_id"], "revoked": False,
                  "broker_server": acc.get("server"),
                  "account_number": acc.get("account_number"),
                  "acquired_at": now,
                  "expires_at": now + timedelta(seconds=LEASE_SECONDS)}},
        upsert=True)
    await advance_ea_deployment(db, doc["account_id"], "TOKEN_CLAIMED",
                                f"installation {installation_id}")
    await advance_ea_deployment(db, doc["account_id"], "TERMINAL_SELECTED",
                                terminal_path)
    return {"installation_id": installation_id,
            "bridge_token": new_token,
            "account_id": doc["account_id"],
            "bridge_endpoint": "/api/bridge",
            # iter-125 correction #2 — broker-reported identity outranks the
            # user-entered account_number until verified_identity exists.
            "permitted_account": (
                (acc.get("verified_identity") or {}).get("account_number")
                or acc.get("broker_account_id_reported")
                or acc.get("account_number")),
            "lease_seconds": LEASE_SECONDS,
            "config_version": "current",
            "connected": False,
            "note": "NOT connected yet — READY_FOR_SHADOW requires a "
                    "verified EA heartbeat from the expected account"}


async def verify_heartbeat_identity(db, acc: dict, *, installation_id: str,
                                    broker_server: str | None = None,
                                    reported_login=None) -> dict:
    """iter-122 Phase 3 — the verified-identity chain for EA heartbeats.

    A heartbeat is AUTHORITATIVE only when ALL of:
      1. the installation_id is recognized (registered, not revoked),
      2. it is bound to THIS account,
      3. the reported broker server matches the registered pairing,
      4. the reported MT5 login matches the expected account number,
      5. the installation currently owns the execution lease.
    Display labels play no part — only broker-verified identity."""
    account_id = str(acc["_id"])
    inst = await db.installations.find_one(
        {"installation_id": installation_id, "revoked": {"$ne": True}})
    if not inst:
        return {"ok": False, "reason":
                f"installation {installation_id} unknown or revoked"}
    if str(inst.get("account_id")) != account_id:
        return {"ok": False,
                "reason": "installation is bound to a different account"}
    configured_server = expected_broker_server(acc)
    if broker_server and configured_server:
        from broker_servers import servers_match
        extra = [p.get("server_names") or []
                 async for p in db.broker_profiles.find(
                     {}, {"server_names": 1}).limit(200)]
        if not servers_match(configured_server, broker_server, extra):
            return {"ok": False, "reason":
                    (f"broker server mismatch: EA reports '{broker_server}', "
                     f"account is registered on '{configured_server}' "
                     "(exact alias-registry match required)")}
    configured_login = str(authoritative_account_number(acc) or "").strip()
    if reported_login is not None and configured_login and \
            configured_login not in ("", "—") and \
            str(reported_login).strip() != configured_login:
        return {"ok": False, "reason":
                (f"MT5 login mismatch: EA reports {reported_login}, "
                 f"expected {configured_login}")}
    lease = await db.execution_leases.find_one({"account_id": account_id})
    owns = (lease and not lease.get("revoked")
            and lease.get("installation_id") == installation_id)
    if not owns:
        return {"ok": False, "reason":
                "installation does not hold the execution lease"}
    return {"ok": True, "installation_id": installation_id}


async def on_ea_heartbeat(db, acc: dict, reported_login=None,
                          installation_id: str | None = None,
                          broker_server: str | None = None,
                          identity_verified: bool | None = None) -> None:
    """Bridge-heartbeat hook: renews the execution-owner lease and drives
    the deployment machine to READY_FOR_SHADOW only when the heartbeat
    matches the expected account (and server, from account config).

    iter-125 (correction #1) — identity is MANDATORY for authority:
    a heartbeat WITHOUT a recognized installation_id NEVER renews the
    execution lease (it may only update non-sensitive telemetry upstream).
    The legacy sub-v1.55 renewal fallback is removed."""
    account_id = str(acc["_id"])
    now = datetime.now(timezone.utc)
    if installation_id:
        inst = await db.installations.find_one(
            {"installation_id": installation_id, "account_id": account_id,
             "revoked": {"$ne": True}})
        lease = await db.execution_leases.find_one({"account_id": account_id})
        owner_ok = (inst is not None and (
            lease is None or lease.get("revoked")
            or lease.get("installation_id") == installation_id))
        if owner_ok:
            await db.execution_leases.update_one(
                {"account_id": account_id},
                {"$set": {"installation_id": installation_id,
                          "user_id": inst["user_id"], "revoked": False,
                          "broker_server": acc.get("server"),
                          "account_number": acc.get("account_number"),
                          "renewed_at": now,
                          "expires_at": now + timedelta(
                              seconds=LEASE_SECONDS)}},
                upsert=True)
    # No installation_id → UNVERIFIED heartbeat → no lease renewal, ever.
    dep = await db.ea_deployments.find_one(
        {"account_id": account_id,
         "state": {"$nin": ["READY_FOR_SHADOW", "FAILED"]}},
        sort=[("created_at", -1)])
    if not dep:
        return
    await advance_ea_deployment(db, account_id, "EA_HEARTBEAT_RECEIVED")
    # READY_FOR_SHADOW requires the FULL verified identity chain when the
    # verification result is known; login/server facts alone no longer
    # promote an unidentified heartbeat.
    if identity_verified is False or not installation_id:
        return
    exp_login = dep.get("expected_login")
    exp_server = dep.get("expected_server")
    login_ok = not exp_login or (
        reported_login is not None
        and str(reported_login) == str(exp_login))
    from broker_servers import servers_match
    server_ok = not exp_server or servers_match(
        exp_server, broker_server or acc.get("server"))
    if login_ok and server_ok:
        await advance_ea_deployment(db, account_id,
                                    "BROKER_ACCOUNT_VERIFIED",
                                    f"login={reported_login}")
        await advance_ea_deployment(db, account_id, "READY_FOR_SHADOW")


async def verify_execution_identity(db, account: dict):
    """iter-125 — LIVE DISPATCH IDENTITY GATE.

    A live trade may only be dispatched when the account's last heartbeat
    carried a fully verified identity chain AND that installation currently
    holds an unexpired, unrevoked execution lease. Paper accounts are
    exempt. Returns None when allowed, else a blocked dict."""
    if (account.get("mode") or "live").lower() == "paper":
        return None
    ident = account.get("ea_identity") or {}
    if not ident.get("authoritative") or not ident.get("installation_id"):
        last = ident.get("reason") or "no identity payload in heartbeat"
        return {"blocked": "identity",
                "reason": ("EA identity unverified — live trading requires "
                           "EA v1.55+ with a paired installation_id "
                           "(dashboard → Accounts → Pair terminal). "
                           f"Last reason: {last}")}
    lease = await db.execution_leases.find_one(
        {"account_id": str(account["_id"])})
    now = datetime.now(timezone.utc)
    if (not lease or lease.get("revoked")
            or lease.get("installation_id") != ident["installation_id"]
            or (_aware(lease.get("expires_at")) or now) <= now):
        return {"blocked": "identity",
                "reason": ("installation does not hold a valid execution "
                           "lease — heartbeat identity must verify to renew "
                           "it (one account → one verified installation → "
                           "one terminal → one lease)")}
    return None
