"""VPS Agent Service — per-account PORTABLE MT5 terminals managed by the STOIC VPS Agent (Easy-Connect Phase 2).

Model (operator-approved): one golden portable MT5 per VPS (`C:\\STOIC\\golden\\MT5`, WebRequest URL allowed once);
the agent clones it to `C:\\STOIC\\MT5\\account-<login>\\` per account, runs the public installer against the clone
(EA, token, STOIC-Server.txt, startup ini), starts it with `/portable`, and keeps it alive: process dead → start;
EA heartbeat stale > STALE_S (asked from here) → graceful restart, at most MAX_RESTARTS_PER_HOUR, then an ops alert.
Install is DASHBOARD-driven: the user clicks "Install on my VPS" → `queue_install_terminal` issues a fresh pairing
token and queues an `install_terminal` command the agent polls. The MT5 password is typed ONCE on the VPS
(`Set-StoicTerminalLogin`, DPAPI) and never reaches this server.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from bson import ObjectId

STALE_S = 180                   # EA heartbeat older than this → the agent restarts the terminal
MAX_RESTARTS_PER_HOUR = 3       # beyond this the agent stops restarting and we alert
AGENT_ONLINE_S = 180            # agent heartbeat age for "online" in the dashboard
TERMINAL_STATUSES = ("queued", "installing", "awaiting_login", "running", "restarted", "restart_loop", "failed", "stopped")
ALERT_KIND = "vps_terminal_restart_loop"


def _now():
    return datetime.now(timezone.utc)


def _aware(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _age_s(now, v):
    d = _aware(v)
    return (now - d).total_seconds() if d else None


async def list_agents(db, user_id: str) -> list[dict]:
    """The user's enrolled VPS agents, with online state and managed-terminal count (dashboard picker)."""
    now = _now()
    out = []
    async for a in db.vps_agents.find({"user_id": user_id, "revoked": {"$ne": True}}).sort("registered_at", -1):
        age = _age_s(now, a.get("last_heartbeat"))
        facts = a.get("facts") or {}
        out.append({
            "agent_id": a["agent_id"], "deployment_id": a.get("deployment_id"),
            "hostname": facts.get("hostname") or (a.get("last_metrics") or {}).get("hostname"),
            "agent_version": facts.get("agent_version"),
            "last_heartbeat": _aware(a.get("last_heartbeat")).isoformat() if a.get("last_heartbeat") else None,
            "online": age is not None and age <= AGENT_ONLINE_S,
            "golden_ready": bool((a.get("last_metrics") or {}).get("golden_ready")),
            "terminals": await db.mt5_instances.count_documents({"agent_id": a["agent_id"]}),
        })
    return out


async def queue_install_terminal(db, user: dict, account_id: str, agent_id: str, server_url: str,
                                 chart_symbol: str = "EURUSD") -> dict:
    """Issue a fresh single-use pairing token for the account and queue `install_terminal` for the agent.
    One account ↔ one terminal: a second install for the same account replaces the pending command."""
    from vps_pathb import queue_command
    account = await db.accounts.find_one({"_id": ObjectId(account_id)})
    if not account or account.get("user_id") != user["id"]:
        raise ValueError("account not found")
    if account.get("mode") == "paper":
        raise ValueError("paper accounts have no MT5 terminal")
    agent = await db.vps_agents.find_one({"agent_id": agent_id, "user_id": user["id"], "revoked": {"$ne": True}})
    if not agent:
        raise ValueError("agent not found")
    if _age_s(_now(), agent.get("last_heartbeat")) is None or _age_s(_now(), agent.get("last_heartbeat")) > AGENT_ONLINE_S:
        raise ValueError("agent offline — start the STOIC VPS Agent on that VPS first")
    from routes.setup_routes import PAIRING_TTL_MINUTES
    token = secrets.token_urlsafe(24)
    now = _now()
    await db.pairing_tokens.delete_many({"account_id": account_id, "consumed_at": {"$exists": False}})
    await db.pairing_tokens.insert_one({
        "token": token, "account_id": account_id, "user_id": user["id"],
        "issued_at": now.isoformat(), "expires_at": (now + timedelta(minutes=PAIRING_TTL_MINUTES)).isoformat(),
        "issued_for": f"vps_agent:{agent_id}",
    })
    await db.agent_commands.update_many(
        {"agent_id": agent_id, "command": "install_terminal", "params.account_id": account_id, "status": "queued"},
        {"$set": {"status": "superseded"}})
    login = str(account.get("account_number") or "")
    res = await queue_command(db, user["id"], agent_id, "install_terminal", {
        "account_id": account_id, "login": login, "server": account.get("server") or "",
        "broker": account.get("broker") or "", "pairing_token": token, "server_url": server_url,
        "chart_symbol": chart_symbol or "EURUSD",
    }, f"user:{user['id']}")
    state = {"status": "queued", "agent_id": agent_id, "command_id": res["command_id"],
             "detail": "waiting for the VPS agent to pick the install up", "updated_at": now.isoformat()}
    await db.accounts.update_one({"_id": account["_id"]}, {"$set": {"vps_terminal": state}})
    return {"ok": True, "command_id": res["command_id"], "agent_id": agent_id, "login": login, "vps_terminal": state}


async def terminals_status(db, agent: dict) -> dict:
    """What the agent's watchdog needs: for each terminal it manages, is the EA heartbeat fresh?"""
    now = _now()
    out = []
    async for inst in db.mt5_instances.find({"agent_id": agent["agent_id"]}):
        acc = None
        if inst.get("account_id"):
            try:
                acc = await db.accounts.find_one({"_id": ObjectId(inst["account_id"])},
                                                 projection={"last_heartbeat": 1, "installer_paired_at": 1, "enabled": 1})
            except Exception:  # noqa: BLE001
                acc = None
        hb_age = _age_s(now, (acc or {}).get("last_heartbeat"))
        out.append({
            "account_id": inst.get("account_id"), "login": inst.get("account_ref"), "directory": inst.get("directory"),
            "status": inst.get("status"), "heartbeat_age_s": None if hb_age is None else round(hb_age),
            "stale": hb_age is None or hb_age > STALE_S,
            "paired": bool((acc or {}).get("installer_paired_at")),
            "restart_wanted": inst.get("status") in ("running", "restarted") and (hb_age is None or hb_age > STALE_S),
        })
    return {"terminals": out, "stale_after_s": STALE_S, "max_restarts_per_hour": MAX_RESTARTS_PER_HOUR, "as_of": now.isoformat()}


async def report_terminal(db, agent: dict, payload: dict) -> dict:
    """Agent → server: one terminal's state. Mirrors a compact `vps_terminal` onto the account for Install
    Progress and raises ONE ops alert when the restart budget is exhausted."""
    login = str(payload.get("login") or payload.get("account_ref") or "").strip()
    account_id = str(payload.get("account_id") or "").strip()
    status = str(payload.get("status") or "").strip()
    if not login or status not in TERMINAL_STATUSES:
        raise ValueError("login and a known status are required")
    now = _now()
    doc = {
        "user_id": agent["user_id"], "agent_id": agent["agent_id"], "deployment_id": agent.get("deployment_id"),
        "account_ref": login, "account_id": account_id or None,
        "directory": str(payload.get("directory") or f"C:\\STOIC\\MT5\\account-{login}\\")[:300],
        "status": status, "detail": str(payload.get("detail") or "")[:500],
        "restarts_last_hour": int(payload.get("restarts_last_hour") or 0),
        "ea_version": payload.get("ea_version"), "mt5_build": payload.get("mt5_build"),
        "pid": payload.get("pid"), "updated_at": now,
    }
    await db.mt5_instances.update_one({"agent_id": agent["agent_id"], "account_ref": login},
                                      {"$set": doc, "$setOnInsert": {"created_at": now}}, upsert=True)
    if account_id:
        try:
            await db.accounts.update_one(
                {"_id": ObjectId(account_id), "user_id": agent["user_id"]},
                {"$set": {"vps_terminal": {"status": status, "agent_id": agent["agent_id"], "detail": doc["detail"],
                                           "directory": doc["directory"], "restarts_last_hour": doc["restarts_last_hour"],
                                           "updated_at": now.isoformat()}}})
        except Exception:  # noqa: BLE001
            pass
    if status == "restart_loop":
        from alerting import raise_alert
        await raise_alert(db, ALERT_KIND, "critical",
                          f"VPS agent {agent['agent_id']} stopped restarting MT5 for login {login}: "
                          f"{MAX_RESTARTS_PER_HOUR} restarts in an hour without a fresh EA heartbeat — "
                          "check the terminal on the VPS (login, WebRequest URL, broker connection).",
                          dedup_key=f"vps_terminal:{agent['agent_id']}:{login}",
                          meta={"agent_id": agent["agent_id"], "login": login, "account_id": account_id})
    return {"ok": True, "login": login, "status": status}
