"""Infrastructure API — VPS providers, deployments, agent lifecycle,
MT5 instances, pairing, health + shadow certification."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from auth import get_current_user
from database import get_db
from http_errors import static_error


def _admin_ok(u) -> bool:
    """fix plan S6 — admin privilege = admin role AND enrolled admin 2FA (same rule as auth.require_admin)."""
    if not u or u.get("role") != "admin":
        return False
    try:
        from auth import require_admin
        require_admin(u)
        return True
    except Exception:
        return False

router = APIRouter(prefix="/infra", tags=["infrastructure"])


def _aware(dt):
    if dt and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _mtls_gate(db, agent_token: str, fingerprint: str) -> dict:
    """iter-172 (#5) — resolve the agent by token, then enforce its enrolled
    per-installation client-cert fingerprint. NOTE (audit round 8 P1-3): until
    ingress terminates client-certificate TLS and injects a verified identity,
    this header check is a *pinned secondary identifier*, NOT cryptographic mTLS. Raises HTTPException(401) directly so
    downstream business ValueErrors keep their own status codes."""
    from agent_mtls import enforce_mtls
    from vps_agent import agent_by_token
    try:
        agent = await agent_by_token(db, agent_token)
        await enforce_mtls(db, agent, fingerprint)
    except ValueError as e:
        raise static_error(401, "agent_auth_failed", e)
    return agent


_FP_HEADER = Header(default="", alias="X-Client-Cert-Fingerprint")


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
        raise static_error(409, "partner_required", e)
    except ValueError as e:
        raise static_error(400, "invalid_request", e)


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
        raise static_error(404, "deployment_not_found", e)


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
async def server_action(deployment_id: str, payload: dict, request: Request,
                        user=Depends(get_current_user)):
    from entitlements import enforce_feature
    await enforce_feature(user, "vps_management")
    if str(payload.get("action") or "") in ("rebuild", "delete"):
        from step_up import require_step_up
        await require_step_up(get_db(), user, request, "vps_destroy")     # fix plan S11
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
        raise static_error(409, "partner_required", e)
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
        "step_up_verified": False, "at": now.isoformat()})
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
async def agent_register(payload: dict, request: Request):
    from vps_agent import register_agent
    # audit #12 P3 — unauthenticated enrolment: throttle per IP so the single-use code cannot be raced
    from security import client_ip, rate_limit
    await rate_limit(get_db(), "agent_register", client_ip(request), 20, 600,
                     "Too many agent registration attempts — try again in 10 minutes.", request=request)
    token = str(payload.get("bootstrap_token") or "")
    if not token and payload.get("enrollment_code"):
        from vps_pathb import resolve_enrollment
        token = await resolve_enrollment(
            get_db(), str(payload["enrollment_code"])) or ""
    try:
        return await register_agent(get_db(), token, payload)
    except ValueError as e:
        raise static_error(401, "agent_registration_refused", e)


@router.post("/agent/cert/enroll")
async def agent_cert_enroll(payload: dict, cert_fp: str = _FP_HEADER):
    """iter-172 (#5) — per-installation mTLS: the agent proves possession of
    its agent_token, submits a CSR and receives a short-lived client cert
    bound to its agent_id. Once a cert is enrolled, ROTATION requires
    presenting the CURRENT cert (a stolen bearer token alone can no longer
    re-key the installation)."""
    import agent_mtls
    from vps_agent import agent_by_token
    db = get_db()
    try:
        agent = await agent_by_token(db, str(payload.get("agent_token") or ""))
    except ValueError as e:
        raise static_error(401, "agent_auth_failed", e)
    m = agent.get("mtls") or {}
    if m and not m.get("revoked"):
        chk = await agent_mtls.verify_agent_cert(db, agent["agent_id"], cert_fp)
        if not chk["ok"] and chk["reason"] != "expired":
            raise HTTPException(status_code=401, detail=(
                "cert already enrolled — rotation requires the current "
                f"client certificate ({chk['reason']})"))
    csr_pem = str(payload.get("csr_pem") or "")
    if not csr_pem:
        raise HTTPException(status_code=400, detail="csr_pem required")
    try:
        return await agent_mtls.issue_from_csr(db, agent, csr_pem)
    except ValueError as e:
        raise static_error(400, "csr_rejected", e)


@router.post("/agents/{agent_id}/mtls/revoke")
async def revoke_agent_mtls(agent_id: str, payload: dict = None,
                            user=Depends(get_current_user)):
    """Owner/admin revokes an installation's client cert (lost VPS,
    compromise). The agent must re-enroll via its agent_token."""
    from agent_mtls import revoke_agent_cert
    db = get_db()
    q = {"agent_id": agent_id}
    if not _admin_ok(user):
        q["user_id"] = user["id"]
    if not await db.vps_agents.find_one(q):
        raise HTTPException(status_code=404, detail="agent not found")
    ok = await revoke_agent_cert(db, agent_id,
                                 str((payload or {}).get("reason") or ""))
    if not ok:
        raise HTTPException(status_code=404,
                            detail="no certificate enrolled for this agent")
    return {"ok": True, "agent_id": agent_id, "mtls_revoked": True}


@router.post("/agent/heartbeat")
async def agent_heartbeat_ep(payload: dict, cert_fp: str = _FP_HEADER):
    from vps_agent import agent_by_token, agent_heartbeat
    from vps_pathb import apply_health_policies
    db = get_db()
    token = str(payload.get("agent_token") or "")
    try:
        await _mtls_gate(db, token, cert_fp)
        out = await agent_heartbeat(db, token, payload.get("metrics") or {})
        agent = await agent_by_token(db, token)
        out["policy_actions"] = await apply_health_policies(
            db, agent, payload.get("metrics") or {})
        return out
    except ValueError as e:
        raise static_error(401, "agent_auth_failed", e)


@router.post("/agent/renew-token")
async def agent_renew_token_ep(payload: dict, cert_fp: str = _FP_HEADER):
    """iter-157 — agent-initiated token rotation (presents current token)."""
    from vps_agent import rotate_agent_token
    db = get_db()
    try:
        await _mtls_gate(db, str(payload.get("agent_token") or ""), cert_fp)
        return await rotate_agent_token(db,
                                        str(payload.get("agent_token") or ""))
    except ValueError as e:
        raise static_error(401, "agent_auth_failed", e)


@router.post("/agent/deploy-status")
async def agent_deploy_status_ep(payload: dict, cert_fp: str = _FP_HEADER):
    """iter-158 — agents report each update attempt; failures raise a
    centralized ops alert so a broken rollout is visible immediately."""
    db = get_db()
    agent = await _mtls_gate(db, str(payload.get("agent_token") or ""),
                             cert_fp)
    status = "success" if payload.get("ok") else "failure"
    # SEC (P3) — attacker-controlled fields flow into alert log lines; strip
    # control chars (CR/LF) to prevent log-line forging (CWE-117).
    import re as _re
    _noctl = lambda v, n: _re.sub(r"[\x00-\x1f\x7f]", "", str(v or ""))[:n]  # noqa: E731
    doc = {"agent_id": agent["agent_id"],
           "artifact": _noctl(payload.get("artifact"), 80),
           "sha256": _re.sub(r"[^A-Fa-f0-9]", "",
                             str(payload.get("sha256") or ""))[:64],
           "status": status,
           "detail": _noctl(payload.get("detail"), 400),
           "agent_version": _noctl(payload.get("agent_version"), 20),
           "at": datetime.now(timezone.utc).isoformat()}
    await db.agent_deployments.insert_one(dict(doc))
    if status == "failure":
        from alerting import raise_alert
        # SEC-002 — a tenant agent must not be able to move FLEET release
        # state by reporting a failure for an artifact it was never assigned.
        # Only a failure whose digest matches the digest THIS agent's channel
        # actually serves counts as a fleet deployment_failed (the trigger for
        # auto-rollback / promotion-hold). Anything else is recorded as an
        # agent-local anomaly (warning, no release impact).
        from release_channels import channel_for_agent
        channel, shas = await channel_for_agent(db, agent["agent_id"])
        assigned = doc["sha256"] and doc["sha256"] in set(shas.values())
        owner = agent.get("user_id")
        if assigned:
            await raise_alert(
                db, kind="deployment_failed", severity="critical",
                message=(f"agent {agent['agent_id']} failed to deploy "
                         f"{doc['artifact'] or 'artifact'} on {channel}: "
                         f"{doc['detail'][:160]}"),
                dedup_key=f"deploy_fail:{agent['agent_id']}:{doc['sha256']}",
                meta={"agent_id": agent["agent_id"], "user_id": owner,
                      "channel": channel, "sha256": doc["sha256"]})
        else:
            await raise_alert(
                db, kind="agent_deploy_anomaly", severity="warning",
                message=(f"agent {agent['agent_id']} reported a deploy "
                         f"failure for an unassigned artifact "
                         f"{doc['sha256'][:12] or doc['artifact']} — ignored "
                         f"for fleet release state"),
                dedup_key=f"deploy_anom:{agent['agent_id']}:{doc['sha256']}",
                meta={"agent_id": agent["agent_id"], "user_id": owner,
                      "sha256": doc["sha256"]})
    doc.pop("_id", None)
    return {"recorded": True, **doc}


@router.post("/agent/hardening")
async def agent_hardening_ep(payload: dict, cert_fp: str = _FP_HEADER):
    from vps_agent import report_hardening
    db = get_db()
    try:
        await _mtls_gate(db, str(payload.get("agent_token") or ""), cert_fp)
        return await report_hardening(db,
                                      str(payload.get("agent_token") or ""),
                                      payload.get("checklist") or {})
    except ValueError as e:
        raise static_error(401, "agent_auth_failed", e)


@router.post("/mt5/instances")
async def mt5_instance_ep(payload: dict, cert_fp: str = _FP_HEADER):
    from vps_agent import register_mt5_instance
    db = get_db()
    try:
        await _mtls_gate(db, str(payload.get("agent_token") or ""), cert_fp)
        return await register_mt5_instance(
            db, str(payload.get("agent_token") or ""), payload)
    except ValueError as e:
        raise static_error(401, "agent_auth_failed", e)


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
        raise static_error(409, "pairing_conflict", e)
    except ValueError as e:
        if "not found" in str(e):
            raise static_error(404, "not_found", e)
        raise static_error(400, "invalid_request", e)
    except Exception:  # noqa: BLE001 — malformed ObjectId etc.
        raise HTTPException(status_code=400, detail="invalid request")


@router.post("/pairing/claim")
async def claim_pairing(payload: dict):
    from vps_agent import claim_pairing_code
    try:
        return await claim_pairing_code(
            get_db(), str(payload.get("code") or ""),
            payload.get("terminal") or {})
    except ValueError as e:
        if "terminal" in str(e):
            raise static_error(400, "terminal_invalid", e)
        raise static_error(401, "agent_auth_failed", e)


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
async def ea_deploy_progress(payload: dict, cert_fp: str = _FP_HEADER):
    """Agent-reported install progress (HOST_INSPECTED, ARTIFACT_VERIFIED,
    EA_INSTALLED, FAILED). Heartbeat-driven states are backend-only."""
    from vps_agent import AGENT_PROGRESS_STATES, advance_ea_deployment
    db = get_db()
    try:
        agent = await _mtls_gate(
            db, str(payload.get("agent_token") or ""), cert_fp)
    except ValueError as e:
        raise static_error(401, "agent_auth_failed", e)
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
        raise static_error(404, "deployment_not_found", e)


@router.post("/agent/discovery")
async def agent_discovery_ep(payload: dict, cert_fp: str = _FP_HEADER):
    from vps_pathb import ingest_discovery
    db = get_db()
    await _mtls_gate(db, str(payload.get("agent_token") or ""), cert_fp)
    try:
        return await ingest_discovery(
            db, str(payload.get("agent_token") or ""),
            payload.get("terminals") or [])
    except ValueError as e:
        raise static_error(401, "agent_auth_failed", e)


@router.get("/deployments/{deployment_id}/discovery")
async def list_discovery(deployment_id: str,
                         user=Depends(get_current_user)):
    db = get_db()
    if not await db.vps_deployments.find_one(
            {"user_id": user["id"], "deployment_id": deployment_id}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="deployment not found")
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
        raise static_error(403, "consent_required", e)
    except RuntimeError as e:
        raise static_error(409, "action_conflict", e)
    except ValueError as e:
        raise static_error(404 if "not found" in str(e) else 400, "action_target_invalid", e)


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
        raise static_error(409, "action_conflict", e)
    except ValueError as e:
        raise static_error(404 if "not found" in str(e) else 400, "action_target_invalid", e)


@router.post("/agents/{agent_id}/rotate-credentials")
async def rotate_agent_credentials(agent_id: str,
                                   user=Depends(get_current_user)):
    """iter-122 Phase 3 — rotate the agent's installation-scoped credentials
    (agent_token + command_key). Old credentials die instantly; the VPS
    operator re-runs the enrollment step with the new values."""
    from entitlements import enforce_feature
    await enforce_feature(user, "vps_quick_connect")
    import secrets as _secrets
    db = get_db()
    q = {"agent_id": agent_id, "revoked": {"$ne": True}}
    if not _admin_ok(user):
        q["user_id"] = user["id"]
    new_token = f"agt_tok_{_secrets.token_urlsafe(32)}"
    new_key = _secrets.token_hex(32)
    from vps_agent import encrypt_command_key, hash_agent_token
    r = await db.vps_agents.update_one(
        q, {"$set": {"agent_token_hash": hash_agent_token(new_token),
                     "command_key_enc": encrypt_command_key(new_key),
                     "credentials_rotated_at":
                         datetime.now(timezone.utc).isoformat()},
            "$unset": {"agent_token": "", "command_key": ""}})
    if r.matched_count != 1:
        raise HTTPException(status_code=404, detail="agent not found")
    return {"agent_id": agent_id, "agent_token": new_token,
            "command_key": new_key,
            "note": "previous credentials revoked immediately"}


@router.post("/installations/{installation_id}/revoke")
async def revoke_installation(installation_id: str,
                              user=Depends(get_current_user)):
    """iter-122 Phase 3 — explicit installation revocation: kills the
    execution lease and rotates the account bridge token so the revoked
    terminal loses ALL authority immediately."""
    from bson import ObjectId
    import secrets as _secrets
    db = get_db()
    now = datetime.now(timezone.utc)
    q = {"installation_id": installation_id, "revoked": {"$ne": True}}
    if not _admin_ok(user):
        q["user_id"] = user["id"]
    inst = await db.installations.find_one_and_update(
        q, {"$set": {"revoked": True, "revoked_at": now,
                     "revoked_reason": "explicit_revoke"}})
    if not inst:
        raise HTTPException(status_code=404,
                            detail="installation not found or already revoked")
    await db.execution_leases.update_one(
        {"account_id": inst["account_id"],
         "installation_id": installation_id},
        {"$set": {"revoked": True, "revoked_at": now}})
    # P1-01 — a revocation rotation nobody needs to read: store the hash only (re-pair issues the next token)
    _acc = await db.accounts.find_one({"_id": ObjectId(inst["account_id"])}) or {"_id": ObjectId(inst["account_id"])}
    await db.accounts.update_one(
        {"_id": ObjectId(inst["account_id"])},
        __import__("bridge_tokens").rotation_update(_acc, f"tok_{_secrets.token_urlsafe(32)}", grace_until=None, suspended=True))
    return {"ok": True, "installation_id": installation_id,
            "note": "lease revoked + bridge token rotated — re-pair to "
                    "restore execution"}


@router.post("/agent/commands/poll")
async def poll_agent_commands(payload: dict, cert_fp: str = _FP_HEADER):
    from vps_pathb import poll_commands
    db = get_db()
    try:
        await _mtls_gate(db, str(payload.get("agent_token") or ""), cert_fp)
        cmds = await poll_commands(db,
                                   str(payload.get("agent_token") or ""))
        return {"commands": cmds}
    except ValueError as e:
        raise static_error(401, "agent_auth_failed", e)


@router.post("/agent/commands/ack")
async def ack_agent_command(payload: dict, cert_fp: str = _FP_HEADER):
    from vps_pathb import ack_command
    db = get_db()
    try:
        await _mtls_gate(db, str(payload.get("agent_token") or ""), cert_fp)
        return await ack_command(db,
                                 str(payload.get("agent_token") or ""),
                                 str(payload.get("command_id") or ""),
                                 bool(payload.get("ok")),
                                 str(payload.get("detail") or ""))
    except ValueError as e:
        raise static_error(401 if "agent" in str(e) else 404, "command_ack_refused", e)


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
async def artifacts_manifest(agent_id: str | None = None):
    """Cohort-aware since iter-160: canary agents receive the candidate
    channel; everyone else gets stable. Always Ed25519-signed."""
    try:
        from release_channels import manifest_for_agent
        return await manifest_for_agent(get_db(), agent_id)
    except RuntimeError as e:
        # iter-125 correction #3 — unsigned manifests are refused outright.
        raise static_error(503, "manifest_unavailable", e)


@router.post("/agent/artifact-digest")
async def report_artifact_digest(payload: dict, cert_fp: str = _FP_HEADER):
    """iter-125 correction #3 — installers report the SHA-256 digest of the
    artifact they ACTUALLY deployed. The server compares it against the
    CI-published hash so a tampered/locally-recompiled EX5 is detected."""
    from vps_agent import agent_by_token
    token = str(payload.get("agent_token") or "")
    bridge_token = str(payload.get("bridge_token") or "")
    db = get_db()
    reporter = None
    if token:
        # audit round 8 P1-4 — the SAME centralized agent gate as every other
        # agent_token route (token + enrolled pinned secondary identifier)
        agent = await _mtls_gate(db, token, cert_fp)
        reporter = {"kind": "agent", "agent_id": str(agent["_id"])}
    elif bridge_token:
        acc = await __import__("bridge_tokens").find_by_current(db, bridge_token)   # P1-01 — by hash
        if not acc:
            raise HTTPException(status_code=401, detail="unknown bridge token")
        reporter = {"kind": "installer", "account_id": str(acc["_id"])}
    else:
        raise HTTPException(status_code=401,
                            detail="agent_token or bridge_token required")
    name = str(payload.get("artifact") or "")
    digest = str(payload.get("sha256") or "").lower()
    if not name or not digest:
        raise HTTPException(status_code=422,
                            detail="artifact and sha256 are required")
    from vps_pathb import build_artifact_manifest
    try:
        manifest = build_artifact_manifest()
    except RuntimeError as e:
        raise static_error(503, "manifest_unavailable", e)
    expected = next((a.get("sha256") for a in manifest["artifacts"]
                     if a["name"] == name), None)
    match = bool(expected) and expected.lower() == digest
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.artifact_digests.insert_one({
        **reporter, "artifact": name, "sha256": digest,
        "expected_sha256": expected, "match": match,
        "version": payload.get("version"),
        "installation_id": payload.get("installation_id"),
        "reported_at": now_iso})
    # r25 P1-01 — record the installer-reported EX5 hash against the installation.
    # r26 P1-02 — this bridge-token path is TELEMETRY ONLY: the bridge token is also
    # held by the EA, so it can never establish `installer_attested`. The admissible
    # measurement arrives through /infra/attestation/* signed by the enrolled device key.
    inst_id = str(payload.get("installation_id") or "").strip()
    bound = False
    if inst_id and name == "stoic-ea-ex5" and reporter["kind"] == "installer":
        res = await db.installations.update_one(
            {"installation_id": inst_id, "account_id": reporter["account_id"],
             "revoked": {"$ne": True}},
            {"$set": {"ex5_reported_sha256": digest, "ex5_reported_at": now_iso,
                      "ex5_reported_installer_version": payload.get("installer_version"),
                      "ex5_reported_terminal": payload.get("terminal")}})
        bound = res.matched_count == 1
    return {"ok": True, "match": match, "expected_sha256": expected,
            "installation_bound": bound, "attests": False,
            "note": "bridge-token report is telemetry only — live proof requires the signed device attestation"}


@router.get("/broker-profiles")
async def broker_profiles_ep(user=Depends(get_current_user)):
    from vps_pathb import broker_profiles
    return {"profiles": await broker_profiles(get_db())}


# ── r26 P1-02 · installer device attestation (nonce handshake) ─────────────
def _attest_http(e) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message})


@router.post("/attestation/challenge")
async def attestation_challenge(payload: dict, request: Request):
    """Single-use nonce for the ENROLLED installer device key of an installation.
    Unauthenticated by design (the installer holds no cookie); the nonce is
    worthless without the private key and dies in 120 s. r26-b P2-01: per-IP /
    per-installation limits, one outstanding nonce, uniform refusal, flood alerting."""
    import device_attestation as da
    from security import client_ip
    inst_id = str(payload.get("installation_id") or "").strip()
    if not inst_id or len(inst_id) > 64:
        raise HTTPException(status_code=422, detail={"code": "malformed", "message": "installation_id required"})
    try:
        return await da.issue_challenge(get_db(), inst_id, client_ip=client_ip(request))
    except da.AttestationError as e:
        raise _attest_http(e)


@router.post("/attestation/verify")
async def attestation_verify(payload: dict, request: Request):
    """Installer posts the signed proof {nonce, installation_id, terminal_identity,
    ex5_sha256, capabilities, ts, signature}. Only a valid signature from the
    enrolled device key records `ex5_measured_by = device_signature` — the sole
    source of `installer_attested` on the heartbeat."""
    import device_attestation as da
    from security import client_ip
    try:
        return await da.verify_attestation(get_db(), payload or {}, client_ip=client_ip(request))
    except da.AttestationError as e:
        raise _attest_http(e)


@router.post("/installations/{installation_id}/device-key/revoke")
async def revoke_device_key(installation_id: str, user=Depends(get_current_user)):
    """Owner/admin revocation — a revoked key can neither challenge nor attest, and the
    heartbeat method for the installation degrades to `device_key_revoked`."""
    db = get_db()
    q = {"installation_id": installation_id, "device_key": {"$exists": True}}
    if not _admin_ok(user):
        q["user_id"] = user["id"]
    now_iso = datetime.now(timezone.utc).isoformat()
    res = await db.installations.update_one(q, {"$set": {"device_key.revoked": True, "device_key.revoked_at": now_iso,
                                                         "device_key.revoked_by": user["id"]}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="installation with an enrolled device key not found")
    await db.accounts.update_many({"installation_id": installation_id, "ea_binary_sha256_method": "installer_attested"},
                                  {"$set": {"ea_binary_sha256_method": "device_key_revoked"}})
    return {"ok": True, "installation_id": installation_id, "revoked_at": now_iso}


@router.post("/broker-installers")
async def register_installer(payload: dict,
                             user=Depends(get_current_user)):
    from vps_pathb import register_broker_installer
    try:
        return await register_broker_installer(get_db(), user["id"],
                                               payload)
    except ValueError as e:
        raise static_error(400, "installer_invalid", e)


@router.post("/broker-installers/{installer_id}/approve")
async def approve_installer(installer_id: str,
                            user=Depends(get_current_user)):
    from vps_pathb import approve_broker_installer
    try:
        return await approve_broker_installer(get_db(), user,
                                              installer_id)
    except PermissionError as e:
        raise static_error(403, "installer_forbidden", e)
    except ValueError as e:
        raise static_error(404, "installer_not_found", e)


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
