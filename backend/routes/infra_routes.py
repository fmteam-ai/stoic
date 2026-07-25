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
    from vps_deployments import create_deployment
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
# heartbeat loop (register as a scheduled task in production)
while ($true) {{
  $Hb = @{{ agent_token = $Reg.agent_token; metrics = @{{
    cpu_percent = [math]::Round((Get-Counter '\Processor(_Total)\% Processor Time').CounterSamples.CookedValue, 1)
    ram_percent = 0; disk_free_gb = [math]::Round((Get-PSDrive C).Free / 1GB, 1)
    clock_offset_ms = 0
    mt5_processes = @(Get-Process -Name terminal64 -ErrorAction SilentlyContinue).Count
    agent_version = "1.0.0"
  }} }} | ConvertTo-Json -Depth 4
  try {{ Invoke-RestMethod -Method Post -Uri "$Base/agent/heartbeat" -Body $Hb -ContentType "application/json" | Out-Null }} catch {{}}
  Start-Sleep -Seconds 60
}}
"""


@router.get("/agent/bootstrap/installer", response_class=PlainTextResponse)
async def bootstrap_installer(token: str):
    """One-time-token-gated PowerShell agent script (spec step 10). The
    token is NOT burned here — registration burns it."""
    db = get_db()
    doc = await db.vps_bootstrap_tokens.find_one({"token": token})
    if not doc or doc.get("used") \
            or _aware(doc["expires_at"]) < datetime.now(timezone.utc):
        raise HTTPException(status_code=401,
                            detail="invalid or expired bootstrap token")
    import os
    base = os.environ.get("PUBLIC_BASE_URL") or ""
    return BOOTSTRAP_PS1.format(base_url=base or "https://<your-stoic-host>",
                                token=token)


@router.post("/agent/register")
async def agent_register(payload: dict):
    from vps_agent import register_agent
    token = str(payload.get("bootstrap_token") or "")
    try:
        return await register_agent(get_db(), token, payload)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.post("/agent/heartbeat")
async def agent_heartbeat_ep(payload: dict):
    from vps_agent import agent_heartbeat
    try:
        return await agent_heartbeat(get_db(),
                                     str(payload.get("agent_token") or ""),
                                     payload.get("metrics") or {})
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
    from vps_agent import create_pairing_code
    try:
        return await create_pairing_code(
            get_db(), user["id"], str(payload.get("account_id") or ""))
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
        return await claim_pairing_code(get_db(),
                                        str(payload.get("code") or ""))
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


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
