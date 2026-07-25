"""STOIC Agent lifecycle — bootstrap tokens, registration, scoped agent
tokens (mTLS later), heartbeats, hardening report, MT5 instance registry
and EA pairing codes (infra spec steps 9-20)."""
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone

BOOTSTRAP_TTL_MIN = 20
PAIRING_TTL_MIN = 10

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


async def register_agent(db, token: str, facts: dict) -> dict:
    """Bootstrap token → unique agent id + scoped revocable agent token
    (fingerprint-bound). The bootstrap token is revoked here (step 11)."""
    boot = await consume_bootstrap_token(db, token)
    now = datetime.now(timezone.utc)
    agent_id = f"agt_{uuid.uuid4().hex[:12]}"
    agent_token = f"agt_tok_{secrets.token_urlsafe(32)}"
    await db.vps_agents.insert_one({
        "agent_id": agent_id, "agent_token": agent_token,
        "user_id": boot["user_id"],
        "deployment_id": boot["deployment_id"],
        "fingerprint": facts.get("machine_fingerprint"),
        "facts": {k: facts.get(k) for k in (
            "agent_version", "windows_version", "cpu", "ram_gb",
            "disk_free_gb", "public_ip", "timezone", "clock_offset_ms")},
        "hardening": {}, "revoked": False,
        "registered_at": now, "last_heartbeat": None})
    return {"agent_id": agent_id, "agent_token": agent_token,
            "heartbeat_interval_sec": 60,
            "capabilities": ["heartbeat", "hardening", "mt5_install",
                             "ea_install", "health_check"],
            "note": "scoped token auth — mTLS enrollment planned"}


async def agent_by_token(db, agent_token: str) -> dict:
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
        "mt5_processes", "agent_version")}
    await db.vps_agents.update_one(
        {"_id": agent["_id"]},
        {"$set": {"last_heartbeat": now, "last_metrics": hb}})
    return {"ok": True, "next_heartbeat_sec": 60}


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


async def create_pairing_code(db, user_id: str, account_id: str) -> dict:
    """One-time EA pairing code, 10-min TTL, scoped to ONE account
    (spec step 19)."""
    from bson import ObjectId
    acc = await db.accounts.find_one(
        {"_id": ObjectId(account_id), "user_id": user_id})
    if not acc:
        raise ValueError("account not found")
    code = _pairing_code()
    now = datetime.now(timezone.utc)
    await db.ea_pairing_codes.insert_one({
        "code": code, "user_id": user_id, "account_id": account_id,
        "used": False, "created_at": now,
        "expires_at": now + timedelta(minutes=PAIRING_TTL_MIN)})
    return {"code": code, "expires_in_min": PAIRING_TTL_MIN,
            "account_id": account_id}


async def claim_pairing_code(db, code: str) -> dict:
    """EA exchanges the pairing code for its account-scoped bridge
    credential + endpoint. Single use."""
    from bson import ObjectId
    doc = await db.ea_pairing_codes.find_one({"code": code})
    if not doc:
        raise ValueError("invalid pairing code")
    if doc.get("used"):
        raise ValueError("pairing code already used")
    if _aware(doc["expires_at"]) < datetime.now(timezone.utc):
        raise ValueError("pairing code expired")
    r = await db.ea_pairing_codes.update_one(
        {"_id": doc["_id"], "used": False},
        {"$set": {"used": True, "used_at": datetime.now(timezone.utc)}})
    if r.modified_count != 1:
        raise ValueError("pairing code already used")
    acc = await db.accounts.find_one({"_id": ObjectId(doc["account_id"])})
    if not acc:
        raise ValueError("account no longer exists")
    return {"bridge_token": acc.get("bridge_token"),
            "account_id": doc["account_id"],
            "bridge_endpoint": "/api/bridge",
            "permitted_account": acc.get("broker_account_id_reported")
            or acc.get("label"),
            "config_version": "current"}
