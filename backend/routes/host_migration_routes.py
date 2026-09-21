"""Admin · Host Migration wizard — proxies to the `migrator` ops sidecar
(docker-compose.migrator.yml). The API never touches docker/SSH itself:
it authenticates the admin, enforces step-up MFA on destructive steps,
forwards to the sidecar with the shared METRICS_TOKEN and journals every
action to `host_migration_events`.

    GET  /api/admin/host-migration/status
    GET  /api/admin/host-migration/public-key
    POST /api/admin/host-migration/scan        {host,port}      → observed fingerprints (no auth, nothing trusted)
    POST /api/admin/host-migration/preflight   {host,user,port,path,accept_fingerprint}   (step-up; fingerprint mandatory)
    POST /api/admin/host-migration/disable-missing                (step-up; audited cutover exception)
    POST /api/admin/host-migration/start                          (step-up)
    POST /api/admin/host-migration/advance     {step}             (step-up)
    POST /api/admin/host-migration/retry
    POST /api/admin/host-migration/abort                          (step-up)
"""
import os
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth import get_current_user, require_admin
from database import get_db

router = APIRouter(tags=["admin"])
STEP_UP_ACTION = "host_migration"
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_\-]{0,31}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._/\-]{1,200}$")


class ScanBody(BaseModel):
    host: str
    port: int = Field(22, ge=1, le=65535)


class PreflightBody(BaseModel):
    host: str
    user: str = "stoic"
    port: int = Field(22, ge=1, le=65535)
    path: str = "/home/stoic/stoic"
    accept_fingerprint: str = Field(..., min_length=10, max_length=120,
                                    description="exact host-key fingerprint confirmed out-of-band")


class AdvanceBody(BaseModel):
    step: str


def _migrator_url() -> str:
    url = os.environ.get("MIGRATOR_URL")
    if not url:
        raise HTTPException(status_code=503, detail={
            "code": "migrator_not_enabled",
            "message": "The host-migration sidecar is not enabled on this server. "
                       "Run `make migrator-on` in the checkout, then reload this page."})
    return url.rstrip("/")


def _migrator_token() -> str:
    """Dedicated one-time migration token (P1-5) — never METRICS_TOKEN."""
    f = os.environ.get("MIGRATOR_TOKEN_FILE")
    if f:
        try:
            return open(f).read().strip()
        except OSError:
            pass
    return os.environ.get("MIGRATOR_TOKEN", "")


def _headers() -> dict:
    tok = _migrator_token()
    if not tok:
        raise HTTPException(status_code=503, detail={"code": "migrator_token_missing",
                                                     "message": "no migration token — run `make migrator-on`"})
    return {"X-Migrator-Token": tok}


async def _call(method: str, path: str, body: dict | None = None, timeout: float = 20.0):
    url = _migrator_url() + path
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, url, json=body, headers=_headers())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail={"code": "migrator_unreachable",
                                                     "message": f"migrator sidecar unreachable: {e}"})
    if r.status_code >= 400:
        try:
            det = r.json()
        except ValueError:
            det = {"error": r.text[:500]}
        if r.status_code == 410:
            det = {"code": "migrator_expired", **det}
        raise HTTPException(status_code=r.status_code if r.status_code in (409, 410) else 502, detail=det)
    return r.json()


async def _journal(user: dict, action: str, detail: dict | None = None):
    await get_db().host_migration_events.insert_one({
        "at": datetime.now(timezone.utc).isoformat(), "actor": user.get("email"),
        "user_id": user.get("id"), "action": action, "detail": detail or {}})


async def _step_up(request: Request, user: dict):
    from step_up import audit_event, require_step_up
    db = get_db()
    await require_step_up(db, user, request, STEP_UP_ACTION)
    await audit_event(db, user["id"], STEP_UP_ACTION, {"path": str(request.url.path)},
                      request, step_up=True)


@router.get("/admin/host-migration/status")
async def hm_status(user=Depends(get_current_user)):
    require_admin(user)
    return {"enabled": True, "state": await _call("GET", "/status")}


@router.get("/admin/host-migration/public-key")
async def hm_public_key(user=Depends(get_current_user)):
    require_admin(user)
    return await _call("GET", "/public-key")


@router.post("/admin/host-migration/scan")
async def hm_scan(body: ScanBody, user=Depends(get_current_user)):
    """Phase 1 — observe host keys only (no authentication, nothing trusted)."""
    require_admin(user)
    if not HOST_RE.match(body.host):
        raise HTTPException(status_code=422, detail="invalid host")
    scan = await _call("POST", "/scan", {"host": body.host, "port": body.port}, timeout=60.0)
    await _journal(user, "host_key_observed", {"host": body.host, "port": body.port,
                                               "fingerprints": [f["fingerprint"] for f in scan.get("fingerprints", [])]})
    return scan


@router.post("/admin/host-migration/preflight")
async def hm_preflight(body: PreflightBody, request: Request, user=Depends(get_current_user)):
    """Phase 2 — admin confirmed ONE exact fingerprint out-of-band (fresh
    step-up MFA); only then known_hosts is pinned and SSH is used."""
    require_admin(user)
    if not HOST_RE.match(body.host) or not USER_RE.match(body.user) or not PATH_RE.match(body.path):
        raise HTTPException(status_code=422, detail="invalid host, user or path")
    if not re.match(r"^(SHA256:[A-Za-z0-9+/=]{40,50}|MD5:([0-9a-f]{2}:){15}[0-9a-f]{2})$", body.accept_fingerprint):
        raise HTTPException(status_code=422, detail="accept_fingerprint must be an exact SHA256:… host-key fingerprint")
    await _step_up(request, user)
    target = {"host": body.host, "user": body.user, "port": body.port, "path": body.path}
    facts = await _call("POST", "/preflight", {"target": target, "accept_fingerprint": body.accept_fingerprint},
                        timeout=240.0)
    await _journal(user, "host_key_accepted", {"host": body.host, "accepted": body.accept_fingerprint,
                                               "observed": (facts.get("host_key") or {}).get("fingerprints")})
    await _journal(user, "preflight", {"target": target, "ok": facts.get("ok"),
                                       "checks": (facts.get("target") or {}).get("checks")})
    return facts


@router.post("/admin/host-migration/disable-missing")
async def hm_disable_missing(request: Request, user=Depends(get_current_user)):
    """Audited EXCEPTION — set the expected accounts that never reconnected
    on the new host to trading_enabled=false so decommission can proceed
    with those accounts explicitly OFF. Step-up + separate journal entry."""
    require_admin(user)
    await _step_up(request, user)
    res = await _call("POST", "/disable-missing", {}, timeout=180.0)
    await _journal(user, "cutover_exception_disable_missing", {"disabled_account_ids": res.get("disabled")})
    return res


@router.post("/admin/host-migration/start")
async def hm_start(request: Request, user=Depends(get_current_user)):
    require_admin(user)
    await _step_up(request, user)
    st = await _call("POST", "/start")
    await _journal(user, "start", {"migration_id": st.get("id")})
    return st


@router.post("/admin/host-migration/advance")
async def hm_advance(body: AdvanceBody, request: Request, user=Depends(get_current_user)):
    require_admin(user)
    if body.step not in ("freeze", "cutover_check", "decommission"):
        raise HTTPException(status_code=422, detail="unknown step")
    if body.step != "cutover_check":
        await _step_up(request, user)
    st = await _call("POST", "/advance", {"step": body.step})
    await _journal(user, f"advance:{body.step}", {"migration_id": st.get("id")})
    return st


@router.post("/admin/host-migration/retry")
async def hm_retry(user=Depends(get_current_user)):
    require_admin(user)
    st = await _call("POST", "/retry")
    await _journal(user, "retry", {"migration_id": st.get("id")})
    return st


@router.post("/admin/host-migration/abort")
async def hm_abort(request: Request, user=Depends(get_current_user)):
    require_admin(user)
    await _step_up(request, user)
    res = await _call("POST", "/abort", timeout=600.0)
    await _journal(user, "abort", {"restarted_source": res.get("restarted_source")})
    return res


@router.get("/admin/host-migration/events")
async def hm_events(limit: int = 50, user=Depends(get_current_user)):
    require_admin(user)
    rows = await get_db().host_migration_events.find({}, {"_id": 0}).sort("at", -1) \
        .limit(min(max(limit, 1), 200)).to_list(length=200)
    return {"events": rows}
