"""VPS Agent Service routes — dashboard side (user) and agent side (agent token)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from auth import get_current_user
from database import get_db
from http_errors import static_error

router = APIRouter(prefix="/vps", tags=["vps-agent"])
_FP_HEADER = Header(default="", alias="X-Client-Cert-Fingerprint")


class InstallTerminalRequest(BaseModel):
    account_id: str = Field(min_length=12, max_length=40)
    chart_symbol: str = Field(default="EURUSD", max_length=24)


@router.get("/agents")
async def my_agents(user=Depends(get_current_user)):
    from vps_terminals import list_agents
    return {"agents": await list_agents(get_db(), user["id"])}


@router.post("/agents/{agent_id}/install-terminal")
async def install_terminal(agent_id: str, payload: InstallTerminalRequest, request: Request,
                           user=Depends(get_current_user)):
    from connect_service import _base_url
    from entitlements import enforce_feature
    from vps_terminals import queue_install_terminal
    await enforce_feature(user, "vps_quick_connect")   # same entitlement as the other agent commands
    base = _base_url(request)
    if not base.startswith("https://"):
        raise static_error(503, "server_url_not_https", RuntimeError("PUBLIC_BASE_URL must be https for a VPS install"))
    try:
        return await queue_install_terminal(get_db(), user, payload.account_id, agent_id, base, payload.chart_symbol)
    except ValueError as e:
        raise static_error(404 if "not found" in str(e) else 409, "vps_install_refused", e)


class RestartTerminalRequest(BaseModel):
    account_id: str = Field(min_length=12, max_length=40)


@router.post("/agents/{agent_id}/restart-terminal")
async def restart_terminal(agent_id: str, payload: RestartTerminalRequest, user=Depends(get_current_user)):
    from entitlements import enforce_feature
    from vps_terminals import queue_restart_terminal
    await enforce_feature(user, "vps_quick_connect")
    try:
        return await queue_restart_terminal(get_db(), user, payload.account_id, agent_id)
    except ValueError as e:
        raise static_error(404 if "not found" in str(e) else 409, "vps_restart_refused", e)


async def _agent(db, payload: dict, cert_fp: str) -> dict:
    from routes.infra_routes import _mtls_gate
    from vps_agent import agent_by_token
    token = str(payload.get("agent_token") or "")
    try:
        await _mtls_gate(db, token, cert_fp)
        return await agent_by_token(db, token)
    except ValueError as e:
        raise static_error(401, "agent_auth_failed", e)


@router.post("/agent/terminals/status")
async def agent_terminals_status(payload: dict, cert_fp: str = _FP_HEADER):
    from vps_terminals import terminals_status
    db = get_db()
    return await terminals_status(db, await _agent(db, payload, cert_fp))


@router.post("/agent/terminals/report")
async def agent_terminal_report(payload: dict, cert_fp: str = _FP_HEADER):
    from vps_terminals import report_terminal
    db = get_db()
    agent = await _agent(db, payload, cert_fp)
    try:
        return await report_terminal(db, agent, payload)
    except ValueError as e:
        raise static_error(400, "terminal_report_invalid", e)
