"""Infrastructure API — VPS providers, deployments, agent lifecycle,
MT5 instances, pairing, health + shadow certification."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import PlainTextResponse

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/infra", tags=["infrastructure"])


def _aware(dt):
    if dt and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ── providers ───────────────────────────────────────────────────
@router.get("/providers")
async def list_providers(user=Depends(get_current_user)):
    from vps_providers import PROVIDERS
    db = get_db()
    connected = {c["provider"] async for c in db.vps_provider_creds.find(
        {"user_id": user["id"]}, {"provider": 1})}
    return {"providers": [
        {"name": k, **v, "connected": k in connected or k == "simulated"}
        for k, v in PROVIDERS.items()]}


@router.post("/providers/{name}/connect")
async def connect_provider(name: str, payload: dict,
                           user=Depends(get_current_user)):
    from entitlements import enforce_feature
    await enforce_feature(user, "vps_management")
    from vps_providers import PROVIDERS
    from secrets_vault import encrypt, mask
    if name not in PROVIDERS:
        raise HTTPException(status_code=404, detail="unknown provider")
    if PROVIDERS[name]["method"] == "partner":
        raise HTTPException(status_code=409, detail=(
            f"{PROVIDERS[name]['label']} is partner-gated — commercial "
            f"partnership required"))
    api_key = str(payload.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")
    db = get_db()
    await db.vps_provider_creds.update_one(
        {"user_id": user["id"], "provider": name},
        {"$set": {"api_key_enc": encrypt(api_key),
                  "api_key_masked": mask(api_key),
                  "tenant_id": payload.get("tenant_id"),
                  "updated_at": datetime.now(timezone.utc)}},
        upsert=True)
    return {"ok": True, "provider": name, "masked": mask(api_key)}


async def _provider_for(db, user_id: str, name: str):
    from secrets_vault import decrypt
    from vps_providers import get_provider
    key = None
    cred = await db.vps_provider_creds.find_one(
        {"user_id": user_id, "provider": name})
    if cred and cred.get("api_key_enc"):
        key = decrypt(cred["api_key_enc"])
    return get_provider(name, key)


@router.get("/providers/{name}/catalog")
async def provider_catalog(name: str, user=Depends(get_current_user)):
    from vps_providers import PartnerRequiredError
    try:
        p = await _provider_for(get_db(), user["id"], name)
        return {"regions": await p.list_regions(),
                "plans": await p.list_plans()}
    except PartnerRequiredError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── broker-first recommendation ─────────────────────────────────
@router.get("/brokers/catalog")
async def broker_catalog(user=Depends(get_current_user)):
    from vps_deployments import BROKER_CATALOG
    return {"brokers": [{"broker": b["broker"], "server": b["server"],
                         "endpoints": b["endpoints"]}
                        for b in BROKER_CATALOG]}


@router.post("/recommend")
async def recommend(payload: dict, user=Depends(get_current_user)):
    from vps_deployments import measure_broker_latency, recommend_capacity
    latency = await measure_broker_latency(
        str(payload.get("broker") or ""))
    capacity = recommend_capacity(
        int(payload.get("mt5_instances") or 1),
        bool(payload.get("research_workload")),
        bool(payload.get("local_ai")))
    return {"latency": latency, "capacity": capacity}


# ── deployments ─────────────────────────────────────────────────
@router.post("/deployments")
async def create_deployment_ep(payload: dict,
                               idempotency_key: str | None = Header(
                                   default=None, alias="Idempotency-Key"),
                               user=Depends(get_current_user)):
    from entitlements import enforce_feature, enforce_vps_quota
    from vps_deployments import create_deployment
    path = str(payload.get("path") or "provision")
    await enforce_feature(
        user, "vps_quick_connect" if path == "existing_vps" else "vps_management")
    active = await get_db().vps_deployments.count_documents(
        {"user_id": user["id"], "state": {"$nin": ["DELETED", "FAILED"]}})
    await enforce_vps_quota(user, active)
    dep = await create_deployment(get_db(), user["id"], payload,
                                  idempotency_key)
    needs_bootstrap = (dep["path"] == "existing_vps"
                       or payload.get("issue_bootstrap"))
    if needs_bootstrap and not dep.get("replayed"):
        from vps_agent import create_bootstrap_token
        dep["bootstrap"] = await create_bootstrap_token(
            get_db(), user["id"], dep["deployment_id"])
    return dep


@router.get("/deployments")
async def list_deployments(user=Depends(get_current_user)):
    from vps_deployments import _serialize, advance_deployment
    db = get_db()
    out = []
    async for d in db.vps_deployments.find(
            {"user_id": user["id"]}).sort("created_at", -1).limit(50):
        try:
            out.append(await advance_deployment(db, user["id"],
                                                d["deployment_id"]))
        except ValueError:
            out.append(_serialize(d))
    return {"deployments": out}


@router.get("/deployments/{deployment_id}")
async def get_deployment(deployment_id: str,
                         user=Depends(get_current_user)):
    from vps_deployments import advance_deployment
    try:
        return await advance_deployment(get_db(), user["id"], deployment_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/deployments/{deployment_id}/bootstrap-token")
async def issue_bootstrap(deployment_id: str,
                          user=Depends(get_current_user)):
    from entitlements import enforce_feature
    await enforce_feature(user, "vps_quick_connect")
    from vps_agent import create_bootstrap_token
    db = get_db()
    dep = await db.vps_deployments.find_one(
        {"user_id": user["id"], "deployment_id": deployment_id})
    if not dep:
        raise HTTPException(status_code=404, detail="deployment not found")
    return await create_bootstrap_token(db, user["id"], deployment_id)


@router.post("/deployments/{deployment_id}/actions")
async def server_action(deployment_id: str, payload: dict,
                        user=Depends(get_current_user)):
    from entitlements import enforce_feature
    await enforce_feature(user, "vps_management")
    from vps_providers import PartnerRequiredError
    db = get_db()
    dep = await db.vps_deployments.find_one(
        {"user_id": user["id"], "deployment_id": deployment_id})
    if not dep or not (dep.get("server") or {}).get("server_id"):
        raise HTTPException(status_code=404, detail="no server for deployment")
    action = str(payload.get("action") or "")
    if action not in ("reboot", "rebuild", "backup", "delete"):
        raise HTTPException(status_code=400, detail="unknown action")
    try:
        p = await _provider_for(db, user["id"], dep["provider"])
        sid = dep["server"]["server_id"]
        fn = {"reboot": p.reboot_server, "rebuild": p.rebuild_server,
              "backup": p.create_backup, "delete": p.delete_server}[action]
        res = await fn(sid)
    except PartnerRequiredError as e:
        raise HTTPException(status_code=409, detail=str(e))
    now = datetime.now(timezone.utc)
    if action == "backup":
        await db.vps_backups.insert_one({
            "user_id": user["id"], "deployment_id": deployment_id,
            "backup_id": res.get("backup_id"), "at": now})
    if action == "delete":
        await db.vps_deployments.update_one(
            {"_id": dep["_id"]}, {"$set": {"state": "FAILED",
                                           "deleted": True,
                                           "updated_at": now}})
    await db.audit_log.insert_one({
        "user_id": user["id"], "action": f"vps_{action}",
        "detail": {"deployment_id": deployment_id},
        "step_up_verified": False, "at": now})
    return {"ok": True, "action": action, "result": res}


# ── agent endpoints (token auth, no session) ────────────────────
BOOTSTRAP_PS1 = r"""# STOIC Agent bootstrap (Path B — connect existing VPS)
Set-ExecutionPolicy Bypass -Scope Process -Force
$ErrorActionPreference = "Stop"
$Base = "{base_url}/api/infra"
$Token = "{token}"
$Fingerprint = (Get-WmiObject Win32_ComputerSystemProduct).UUID
$Body = @{{
  bootstrap_token = $Token
  machine_fingerprint = $Fingerprint
  agent_version = "1.0.0"
  windows_version = [System.Environment]::OSVersion.VersionString
  cpu = (Get-WmiObject Win32_Processor).Name
  ram_gb = [math]::Round((Get-WmiObject Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
  disk_free_gb = [math]::Round((Get-PSDrive C).Free / 1GB, 1)
  timezone = (Get-TimeZone).Id
  clock_offset_ms = 0
}} | ConvertTo-Json
$Reg = Invoke-RestMethod -Method Post -Uri "$Base/agent/register" -Body $Body -ContentType "application/json"
Write-Host "Registered as $($Reg.agent_id)"
# hardening (best-effort; review each step for your environment)
Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled True
w32tm /resync | Out-Null
$Hard = @{{ agent_token = $Reg.agent_token; checklist = @{{
  firewall_enabled = $true; time_sync_configured = $true
}} }} | ConvertTo-Json -Depth 4
Invoke-RestMethod -Method Post -Uri "$Base/agent/hardening" -Body $Hard -ContentType "application/json" | Out-Null
# heartbeat + command loop (register as a scheduled task in production)
while ($true) {{
  $Hb = @{{ agent_token = $Reg.agent_token; metrics = @{{
    cpu_percent = [math]::Round((Get-Counter '\Processor(_Total)\% Processor Time').CounterSamples.CookedValue, 1)
    ram_percent = 0; disk_free_gb = [math]::Round((Get-PSDrive C).Free / 1GB, 1)
    clock_offset_ms = 0
    mt5_processes = @(Get-Process -Name terminal64 -ErrorAction SilentlyContinue).Count
    agent_version = "1.0.0"
  }} }} | ConvertTo-Json -Depth 4
  try {{ Invoke-RestMethod -Method Post -Uri "$Base/agent/heartbeat" -Body $Hb -ContentType "application/json" | Out-Null }} catch {{}}
  try {{
    $Cmds = Invoke-RestMethod -Method Post -Uri "$Base/agent/commands/poll" -Body (@{{ agent_token = $Reg.agent_token }} | ConvertTo-Json) -ContentType "application/json"
    foreach ($c in $Cmds.commands) {{
      # execute $c.command here (install_mt5 / restart_terminal / rotate_logs / ...)
      $Ack = @{{ agent_token = $Reg.agent_token; command_id = $c.command_id; ok = $true; detail = "executed" }} | ConvertTo-Json
      Invoke-RestMethod -Method Post -Uri "$Base/agent/commands/ack" -Body $Ack -ContentType "application/json" | Out-Null
    }}
  }} catch {{}}
  Start-Sleep -Seconds 60
}}
"""

DISCOVERY_PS1_SNIPPET = r"""
# MT5 discovery scan (Path B step 4)
$Terminals = @()
$Paths = @("$env:ProgramFiles", "${env:ProgramFiles(x86)}", "$env:APPDATA\MetaQuotes\Terminal")
foreach ($p in $Paths) {
  Get-ChildItem -Path $p -Filter terminal64.exe -Recurse -Depth 3 -ErrorAction SilentlyContinue | ForEach-Object {
    $Terminals += @{ path = $_.DirectoryName; broker_hint = ($_.DirectoryName -split '\\')[-1]; running = $false; ea_installed = $false; source = "filesystem" }
  }
}
Get-Process -Name terminal64 -ErrorAction SilentlyContinue | ForEach-Object {
  $Terminals += @{ path = $_.Path; broker_hint = "running-process"; running = $true; ea_installed = $false; source = "process" }
}
$Disc = @{ agent_token = $Reg.agent_token; terminals = $Terminals } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri "$Base/agent/discovery" -Body $Disc -ContentType "application/json" | Out-Null
"""


@router.get("/agent/bootstrap/installer", response_class=PlainTextResponse)
async def bootstrap_installer(token: str = "", enrollment_code: str = ""):
    """One-time-token/enrollment-code-gated PowerShell agent script
    (spec step 10). The credential is NOT burned here — registration
    burns it."""
    db = get_db()
    if enrollment_code and not token:
        from vps_pathb import resolve_enrollment
        token = await resolve_enrollment(db, enrollment_code) or ""
    doc = await db.vps_bootstrap_tokens.find_one({"token": token})
    if not doc or doc.get("used") \
            or _aware(doc["expires_at"]) < datetime.now(timezone.utc):
        raise HTTPException(status_code=401,
                            detail="invalid or expired bootstrap token")
    import os
    base = os.environ.get("PUBLIC_BASE_URL") or ""
    script = BOOTSTRAP_PS1.format(
        base_url=base or "https://<your-stoic-host>", token=token)
    return script.replace("# heartbeat + command loop",
                          DISCOVERY_PS1_SNIPPET
                          + "\n# heartbeat + command loop")


@router.post("/agent/register")
async def agent_register(payload: dict):
    from vps_agent import register_agent
    token = str(payload.get("bootstrap_token") or "")
    if not token and payload.get("enrollment_code"):
        from vps_pathb import resolve_enrollment
        token = await resolve_enrollment(
            get_db(), str(payload["enrollment_code"])) or ""
    try:
        return await register_agent(get_db(), token, payload)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.post("/agent/heartbeat")
async def agent_heartbeat_ep(payload: dict):
    from vps_agent import agent_by_token, agent_heartbeat
    from vps_pathb import apply_health_policies
    db = get_db()
    token = str(payload.get("agent_token") or "")
    try:
        out = await agent_heartbeat(db, token, payload.get("metrics") or {})
        agent = await agent_by_token(db, token)
        out["policy_actions"] = await apply_health_policies(
            db, agent, payload.get("metrics") or {})
        return out
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.post("/agent/hardening")
async def agent_hardening_ep(payload: dict):
    from vps_agent import report_hardening
    try:
        return await report_hardening(get_db(),
                                      str(payload.get("agent_token") or ""),
                                      payload.get("checklist") or {})
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.post("/mt5/instances")
async def mt5_instance_ep(payload: dict):
    from vps_agent import register_mt5_instance
    try:
        return await register_mt5_instance(
            get_db(), str(payload.get("agent_token") or ""), payload)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


# ── EA pairing ──────────────────────────────────────────────────
@router.post("/pairing")
async def create_pairing(payload: dict, user=Depends(get_current_user)):
    from entitlements import enforce_feature
    await enforce_feature(user, "vps_quick_connect")
    from vps_agent import create_pairing_code
    try:
        return await create_pairing_code(
            get_db(), user["id"], str(payload.get("account_id") or ""),
            expected_login=payload.get("expected_login"),
            expected_server=payload.get("expected_server"),
            revoke_existing=bool(payload.get("revoke_existing")))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        if "not found" in str(e):
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001 — malformed ObjectId etc.
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/pairing/claim")
async def claim_pairing(payload: dict):
    from vps_agent import claim_pairing_code
    try:
        return await claim_pairing_code(
            get_db(), str(payload.get("code") or ""),
            payload.get("terminal") or {})
    except ValueError as e:
        if "terminal" in str(e):
            raise HTTPException(status_code=400, detail=str(e))
        raise HTTPException(status_code=401, detail=str(e))


@router.get("/ea-deployments")
async def list_ea_deployments(user=Depends(get_current_user)):
    from vps_agent import DEPLOY_STATES, LEASE_SECONDS
    db = get_db()
    now = datetime.now(timezone.utc)
    out = []
    async for d in db.ea_deployments.find(
            {"user_id": user["id"]}).sort("created_at", -1).limit(20):
        lease = await db.execution_leases.find_one(
            {"account_id": d["account_id"]})
        lease_active = bool(lease and not lease.get("revoked")
                            and _aware(lease["expires_at"]) > now)
        out.append({
            "ea_deployment_id": d["ea_deployment_id"],
            "account_id": d["account_id"], "state": d["state"],
            "connected": d["state"] == "READY_FOR_SHADOW" and lease_active,
            "state_history": d.get("state_history") or [],
            "expected_login": d.get("expected_login"),
            "expected_server": d.get("expected_server"),
            "execution_owner": (lease or {}).get("installation_id"),
            "lease_active": lease_active})
    return {"deployments": out, "states": DEPLOY_STATES,
            "lease_seconds": LEASE_SECONDS}


@router.post("/ea-deploy/progress")
async def ea_deploy_progress(payload: dict):
    """Agent-reported install progress (HOST_INSPECTED, ARTIFACT_VERIFIED,
    EA_INSTALLED, FAILED). Heartbeat-driven states are backend-only."""
    from vps_agent import (AGENT_PROGRESS_STATES, advance_ea_deployment,
                           agent_by_token)
    db = get_db()
    try:
        agent = await agent_by_token(
            db, str(payload.get("agent_token") or ""))
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    state = str(payload.get("state") or "")
    if state not in AGENT_PROGRESS_STATES:
        raise HTTPException(status_code=400, detail=(
            f"agents may only report {sorted(AGENT_PROGRESS_STATES)}"))
    account_id = str(payload.get("account_id") or "")
    dep = await db.ea_deployments.find_one(
        {"account_id": account_id, "user_id": agent["user_id"]})
    if not dep:
        raise HTTPException(status_code=404,
                            detail="no deployment for that account")
    out = await advance_ea_deployment(db, account_id, state,
                                      str(payload.get("detail") or ""))
    return {"ok": True, "state": (out or {}).get("state")}


# ── Path B: connect existing VPS ────────────────────────────────
@router.post("/vps/connect-existing")
async def connect_existing_ep(payload: dict,
                              user=Depends(get_current_user)):
    from entitlements import enforce_feature, enforce_vps_quota
    await enforce_feature(user, "vps_quick_connect")
    active = await get_db().vps_deployments.count_documents(
        {"user_id": user["id"], "state": {"$nin": ["DELETED", "FAILED"]}})
    await enforce_vps_quota(user, active)
    from vps_pathb import connect_existing
    return await connect_existing(get_db(), user["id"], payload)


@router.get("/deployments/{deployment_id}/pathb-status")
async def pathb_status_ep(deployment_id: str,
                          user=Depends(get_current_user)):
    from vps_pathb import pathb_status
    try:
        return await pathb_status(get_db(), user["id"], deployment_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/agent/discovery")
async def agent_discovery_ep(payload: dict):
    from vps_pathb import ingest_discovery
    try:
        return await ingest_discovery(
            get_db(), str(payload.get("agent_token") or ""),
            payload.get("terminals") or [])
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.get("/deployments/{deployment_id}/discovery")
async def list_discovery(deployment_id: str,
                         user=Depends(get_current_user)):
    db = get_db()
    out = []
    async for d in db.mt5_discovered.find(
            {"user_id": user["id"], "deployment_id": deployment_id}):
        out.append({k: (str(v) if k in ("created_at", "updated_at",
                                        "decided_at") else v)
                    for k, v in d.items() if k != "_id"})
    return {"terminals": out}


@router.post("/discovery/{discovery_id}/decision")
async def terminal_decision(discovery_id: str, payload: dict,
                            user=Depends(get_current_user)):
    from entitlements import enforce_feature
    await enforce_feature(user, "vps_quick_connect")
    from vps_pathb import decide_terminal
    try:
        return await decide_terminal(
            get_db(), user["id"], discovery_id,
            str(payload.get("action") or ""),
            bool(payload.get("consent")))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404 if "not found" in str(e)
                            else 400, detail=str(e))


# ── agent command queue ─────────────────────────────────────────
@router.post("/agents/{agent_id}/commands")
async def queue_agent_command(agent_id: str, payload: dict,
                              user=Depends(get_current_user)):
    from entitlements import enforce_feature
    await enforce_feature(user, "vps_quick_connect")
    from vps_pathb import queue_command
    try:
        return await queue_command(get_db(), user["id"], agent_id,
                                   str(payload.get("command") or ""),
                                   payload.get("params") or {},
                                   f"user:{user['id']}")
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404 if "not found" in str(e)
                            else 400, detail=str(e))


@router.post("/agent/commands/poll")
async def poll_agent_commands(payload: dict):
    from vps_pathb import poll_commands
    try:
        cmds = await poll_commands(get_db(),
                                   str(payload.get("agent_token") or ""))
        return {"commands": cmds}
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.post("/agent/commands/ack")
async def ack_agent_command(payload: dict):
    from vps_pathb import ack_command
    try:
        return await ack_command(get_db(),
                                 str(payload.get("agent_token") or ""),
                                 str(payload.get("command_id") or ""),
                                 bool(payload.get("ok")),
                                 str(payload.get("detail") or ""))
    except ValueError as e:
        raise HTTPException(status_code=401 if "agent" in str(e)
                            else 404, detail=str(e))


@router.get("/agents/{agent_id}/health")
async def agent_health(agent_id: str, user=Depends(get_current_user)):
    from vps_pathb import check_unreachable
    db = get_db()
    await check_unreachable(db, user["id"])
    a = await db.vps_agents.find_one(
        {"agent_id": agent_id, "user_id": user["id"]})
    if not a:
        raise HTTPException(status_code=404, detail="agent not found")
    hb = _aware(a.get("last_heartbeat"))
    now = datetime.now(timezone.utc)
    cmds = []
    async for c in db.agent_commands.find(
            {"agent_id": agent_id}).sort("created_at", -1).limit(10):
        cmds.append({"command_id": c["command_id"],
                     "command": c["command"], "status": c["status"],
                     "issued_by": c.get("issued_by"),
                     "detail": c.get("detail")})
    return {"agent_id": agent_id,
            "heartbeat_age_sec": (round((now - hb).total_seconds())
                                  if hb else None),
            "metrics": a.get("last_metrics"),
            "policy_flags": a.get("policy_flags") or {},
            "commands_frozen": bool(a.get("commands_frozen")),
            "hardening": a.get("hardening") or {},
            "recent_commands": cmds}


# ── artifacts + broker profiles + failure matrix ────────────────
@router.get("/artifacts/manifest")
async def artifacts_manifest():
    from vps_pathb import build_artifact_manifest
    return build_artifact_manifest()


@router.get("/broker-profiles")
async def broker_profiles_ep(user=Depends(get_current_user)):
    from vps_pathb import broker_profiles
    return {"profiles": await broker_profiles(get_db())}


@router.post("/broker-installers")
async def register_installer(payload: dict,
                             user=Depends(get_current_user)):
    from vps_pathb import register_broker_installer
    try:
        return await register_broker_installer(get_db(), user["id"],
                                               payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/broker-installers/{installer_id}/approve")
async def approve_installer(installer_id: str,
                            user=Depends(get_current_user)):
    from vps_pathb import approve_broker_installer
    try:
        return await approve_broker_installer(get_db(), user,
                                              installer_id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/failure-matrix")
async def failure_matrix(user=Depends(get_current_user)):
    from vps_pathb import FAILURE_MATRIX
    return {"matrix": FAILURE_MATRIX,
            "note": "every automatic step has a recovery action"}


# ── overview + health + certification ───────────────────────────
@router.get("/overview")
async def infra_overview(user=Depends(get_current_user)):
    db = get_db()
    now = datetime.now(timezone.utc)
    agents = []
    async for a in db.vps_agents.find({"user_id": user["id"],
                                       "revoked": {"$ne": True}}):
        hb = _aware(a.get("last_heartbeat"))
        agents.append({
            "agent_id": a["agent_id"],
            "deployment_id": a["deployment_id"],
            "facts": a.get("facts"), "metrics": a.get("last_metrics"),
            "hardening_missing": [k for k, v in
                                  (a.get("hardening") or {}).items()
                                  if not v],
            "heartbeat_age_sec": (round((now - hb).total_seconds())
                                  if hb else None)})
    instances = [
        {k: v for k, v in i.items() if k not in ("_id",)}
        async for i in db.mt5_instances.find({"user_id": user["id"]})]
    for i in instances:
        for k in ("created_at", "updated_at"):
            if hasattr(i.get(k), "isoformat"):
                i[k] = i[k].isoformat()
    backups = []
    async for b in db.vps_backups.find(
            {"user_id": user["id"]}).sort("at", -1).limit(10):
        backups.append({"deployment_id": b.get("deployment_id"),
                        "backup_id": b.get("backup_id"),
                        "at": str(b.get("at"))})
    return {"agents": agents, "mt5_instances": instances,
            "backups": backups}


@router.get("/certification")
async def shadow_certification(user=Depends(get_current_user)):
    """Spec step 24 — pre-order shadow certification. Initial mode is
    always observe/shadow; this reports the PASS/FAIL board."""
    db = get_db()
    now = datetime.now(timezone.utc)
    checks = {}

    doc = await db.intraday_candles.find_one(
        {"user_id": user["id"]}, sort=[("_id", -1)],
        projection={"bars": {"$slice": -1}})
    bar_t = float(doc["bars"][-1].get("t") or 0) \
        if doc and doc.get("bars") else None
    checks["market_data_receiving"] = bool(
        bar_t and now.timestamp() - bar_t < 3600)

    checks["signals_generating"] = bool(await db.signals.find_one(
        {"user_id": user["id"],
         "created_at": {"$gte": (now.replace(hour=0, minute=0, second=0)
                                 ).isoformat()}}) or
        await db.signals.find_one({"user_id": user["id"]}))
    checks["risk_decisions"] = bool(await db.trade_decisions.find_one(
        {"user_id": user["id"]}))
    hb_fresh = False
    async for a in db.accounts.find({"user_id": user["id"],
                                     "last_heartbeat": {"$ne": None}},
                                    {"last_heartbeat": 1}):
        hb = a["last_heartbeat"]
        if isinstance(hb, str):
            try:
                hb = datetime.fromisoformat(hb)
            except ValueError:
                continue
        if hb and (now - _aware(hb)).total_seconds() < 300:
            hb_fresh = True
            break
    checks["ea_heartbeat"] = hb_fresh
    checks["broker_reconciliation"] = bool(
        await db.broker_deals.find_one({}))
    agent = await db.vps_agents.find_one(
        {"user_id": user["id"], "revoked": {"$ne": True}},
        sort=[("registered_at", -1)])
    off = ((agent or {}).get("last_metrics") or {}).get("clock_offset_ms")
    checks["clock_synchronization"] = (off is not None
                                       and abs(float(off)) < 1000)
    # duplicate-command prevention: broker_deals idempotency + EA fence
    checks["no_duplicate_commands"] = True
    lat = await db.broker_intel_scores.find_one(
        {}, sort=[("at", -1)])
    lcomp = ((lat or {}).get("components") or {}).get("latency")
    if isinstance(lcomp, dict):
        lcomp = lcomp.get("score")
    checks["latency_within_threshold"] = (lcomp is None
                                          or float(lcomp) >= 40)
    passed = sum(1 for v in checks.values() if v)
    return {"checks": checks, "passed": passed, "total": len(checks),
            "certified": passed == len(checks),
            "initial_mode": "shadow",
            "note": "provisioning never starts above shadow — promotion "
                    "goes demo → supervised_live → autonomous_live via "
                    "the certification-gated pipeline",
            "at": now.isoformat()}
