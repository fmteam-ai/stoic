"""Security & Health Agent — admin API (SA1 read-only; SA3 writes with step-up + audit chain)."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from bson import ObjectId
from pydantic import BaseModel

from auth import get_current_user, require_admin
from database import get_db
from security_agent import actions, alerts, config, reports, rules
from security_agent.checks import CHECKS
from security_agent.checks.common import f
from security_agent.findings import open_or_update, set_status
from security import invalidate_block_cache
from step_up import require_step_up

router = APIRouter(prefix="/admin/security", tags=["security-agent"])


def _ser(d: dict) -> dict:
    d = dict(d)
    d["id"] = str(d.pop("_id"))
    if not isinstance(d.get("expires_at"), str):
        d.pop("expires_at", None)                        # TTL datetimes are internal; ISO strings are informative
    return d


@router.get("/status")
async def security_status(user=Depends(get_current_user)):
    require_admin(user)
    db = get_db()
    cfg = await config.load(db)
    open_q = {"status": {"$in": ["open", "contained", "acknowledged"]}}
    by_sev = {s: await db.security_findings.count_documents({**open_q, "severity": s}) for s in config.SEVERITIES}
    cap = await db.security_findings.count_documents({**open_q, "check_id": "containment_cap_reached"})
    light = "red" if by_sev["critical"] or cap else "amber" if by_sev["high"] else "green"
    lease = await db.worker_leases.find_one({"_id": "security"})
    return {"light": light, "mode": cfg["mode"], "rules_enabled": cfg["rules_enabled"], "open_by_severity": by_sev,
            "checks_total": len(CHECKS), "worker_lease": {"expires_at": str((lease or {}).get("expires_at") or ""), "holder": (lease or {}).get("holder")},
            "protected_ips_count": len(cfg["protected_ips"])}


@router.get("/scorecard")
async def observe_scorecard(days: int = 14, user=Depends(get_current_user)):
    """Observe scorecard — per rule: proposals in the window and how many would have hit a real user."""
    require_admin(user)
    db = get_db()
    days = max(1, min(int(days or 14), 90))
    from security_agent.scorecard import build
    return await build(db, await config.load(db), days=days)


@router.get("/findings")
async def list_findings(status: str | None = None, severity: str | None = None, area: str | None = None,
                        limit: int = 100, user=Depends(get_current_user)):
    require_admin(user)
    q: dict = {}
    if status:
        q["status"] = {"$in": status.split(",")}
    else:
        q["status"] = {"$in": ["open", "contained", "acknowledged"]}
    if severity:
        q["severity"] = {"$in": severity.split(",")}
    if area:
        q["area"] = area
    rows = await get_db().security_findings.find(q).sort([("last_seen", -1)]).limit(max(1, min(int(limit), 500))).to_list(length=500)
    order = {s: i for i, s in enumerate(config.SEVERITIES)}
    rows.sort(key=lambda r: -order.get(r.get("severity"), 0))   # stable: most severe first, newest within
    return {"findings": [_ser(r) for r in rows]}


@router.get("/findings/{finding_id}")
async def get_finding(finding_id: str, user=Depends(get_current_user)):
    require_admin(user)
    if not ObjectId.is_valid(finding_id):
        raise HTTPException(status_code=404, detail="finding not found")
    row = await get_db().security_findings.find_one({"_id": ObjectId(finding_id)})
    if not row:
        raise HTTPException(status_code=404, detail="finding not found")
    out = _ser(row)
    # S10 — tell the UI whether a containment action is still active for this finding
    out["action_active"] = bool(await get_db().security_actions.find_one(
        {"kind": "containment", "finding_id": finding_id, "status": "done"}, {"_id": 1}))
    return out


@router.get("/check-runs")
async def check_runs(user=Depends(get_current_user)):
    require_admin(user)
    db = get_db()
    latest = {r["check_id"]: r async for r in db.security_check_runs.find({"_id": {"$regex": "^latest:"}})}
    return {"checks": [{"check_id": cid, "interval_s": iv, **{k: v for k, v in (latest.get(cid) or {}).items() if k != "_id"}}
                       for cid, (_, iv) in CHECKS.items()]}


# ── SA3: actions log, reports, writes (step-up MFA + audit chain) ───────────
async def _audit(db, user, action: str, target: str, meta: dict) -> None:
    from audit_chain import append_chained
    await append_chained(db, {"actor_email": user.get("email"), "actor_id": user.get("id"), "action": action, "target_kind": "security_agent",
                              "target_id": target, "target_label": "", "reason": "", "meta": meta, "at": datetime.now(timezone.utc).isoformat()})


@router.get("/actions")
async def list_actions(limit: int = 100, kind: str | None = None, user=Depends(get_current_user)):
    require_admin(user)
    q = {"kind": kind} if kind else {}
    rows = await get_db().security_actions.find(q).sort([("at", -1)]).limit(max(1, min(int(limit), 500))).to_list(length=500)
    return {"actions": [_ser(r) for r in rows], "rules": {k: {"title": v["title"], "checks": list(v["checks"]), "action": v["action"]} for k, v in rules.RULES.items()}}


@router.get("/blocks")
async def list_blocks(user=Depends(get_current_user)):
    require_admin(user)
    now = datetime.now(timezone.utc)
    rows = await get_db().security_blocks.find({"active": True, "expires_at": {"$gt": now}}).sort([("expires_at", 1)]).limit(500).to_list(length=500)
    out = []
    for r in rows:
        exp = r.get("expires_at")
        if hasattr(exp, "tzinfo") and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)          # motor returns naive UTC datetimes
        d = {k: v for k, v in r.items() if k not in ("_id", "expires_at")}
        d.update(id=str(r["_id"]), expires_at=exp.isoformat() if hasattr(exp, "isoformat") else str(exp),
                 seconds_left=max(0, int((exp - now).total_seconds())) if hasattr(exp, "timestamp") else None)
        out.append(d)
    return {"blocks": out}


class UndoBody(BaseModel):
    note: str = ""


@router.post("/actions/{action_id}/undo")
async def undo_action(action_id: str, body: UndoBody, request: Request, user=Depends(get_current_user)):
    require_admin(user)
    db = get_db()
    await require_step_up(db, user, request, "security_action_undo")
    try:
        row = await actions.undo(db, action_id, user.get("email") or user["id"], body.note)
    except LookupError:
        raise HTTPException(status_code=404, detail={"code": "action_not_found", "message": "security action not found"})
    except ValueError as e:
        from http_errors import static_error
        raise static_error(409, "action_not_undoable", e)
    invalidate_block_cache()
    return _ser(row)


class ExtendBody(BaseModel):
    minutes: int


@router.post("/actions/{action_id}/extend")
async def extend_action(action_id: str, body: ExtendBody, request: Request, user=Depends(get_current_user)):
    require_admin(user)
    db = get_db()
    await require_step_up(db, user, request, "security_action_extend")
    if not 0 <= body.minutes <= 7 * 24 * 60:
        raise HTTPException(status_code=400, detail="minutes must be 0..10080")
    try:
        row = await actions.extend(db, action_id, body.minutes, user.get("email") or user["id"])
    except LookupError:
        raise HTTPException(status_code=404, detail={"code": "action_not_found", "message": "security action not found"})
    invalidate_block_cache()
    return _ser(row)


class StatusBody(BaseModel):
    status: str
    note: str = ""


@router.post("/findings/{finding_id}/status")
async def finding_status(finding_id: str, body: StatusBody, request: Request, user=Depends(get_current_user)):
    require_admin(user)
    db = get_db()
    await require_step_up(db, user, request, "security_finding_status")
    if body.status not in ("acknowledged", "resolved", "false_positive") or not ObjectId.is_valid(finding_id):
        raise HTTPException(status_code=400, detail="status must be acknowledged|resolved|false_positive")
    ok = await set_status(db, ObjectId(finding_id), body.status, user.get("email") or user["id"], body.note)
    if not ok:
        raise HTTPException(status_code=404, detail="finding not open")
    await _audit(db, user, f"security_finding_{body.status}", finding_id, {"note": body.note[:200]})
    return _ser(await db.security_findings.find_one({"_id": ObjectId(finding_id)}))


class ModeBody(BaseModel):
    mode: str
    rules_enabled: list[str] | None = None


@router.post("/mode")
async def set_mode(body: ModeBody, request: Request, user=Depends(get_current_user)):
    require_admin(user)
    db = get_db()
    await require_step_up(db, user, request, "security_agent_mode")
    if body.mode not in config.MODES:
        raise HTTPException(status_code=400, detail="mode must be observe|enforce")
    sets = {"mode": body.mode}
    if body.rules_enabled is not None:
        bad = [r for r in body.rules_enabled if r not in rules.RULES]
        if bad:
            raise HTTPException(status_code=400, detail=f"unknown rules {bad}")
        sets["rules_enabled"] = body.rules_enabled
    await db.platform_state.update_one({"_id": config.STATE_ID}, {"$set": sets}, upsert=True)
    await _audit(db, user, "security_agent_mode", body.mode, sets)
    cfg = await config.load(db)
    return {"mode": cfg["mode"], "rules_enabled": cfg["rules_enabled"]}


@router.post("/test-alert")
async def test_alert(request: Request, user=Depends(get_current_user)):
    """Opens a Critical test finding; the next tick delivers it (acceptance: Telegram + email within 30 s)."""
    require_admin(user)
    db = get_db()
    await require_step_up(db, user, request, "security_test_alert")
    fd = f("agent_test_alert", f"by:{user['id']}", "critical", "platform", f"test alert requested by {user.get('email')}", {"actor": user.get("email")})
    res = await open_or_update(db, fd)
    await _audit(db, user, "security_test_alert", res["id"], {})
    return {"finding_id": res["id"], "created": res["created"], "telegram_configured": alerts.telegram_creds() is not None}


@router.get("/reports/{kind}")
async def get_report(kind: str, format: str = "json", build: bool = False, user=Depends(get_current_user)):
    require_admin(user)
    if kind not in ("daily", "weekly"):
        raise HTTPException(status_code=404, detail="kind must be daily|weekly")
    db = get_db()
    if build:
        cfg = await config.load(db)
        rep = await (reports.build_daily if kind == "daily" else reports.build_weekly)(db, cfg)
        rep = {**rep, "html": reports.render_html(rep), "text": reports.render_text(rep), "_id": "preview"}
    else:
        rep = await db.security_reports.find_one({"kind": kind}, sort=[("built_at", -1)])
        if not rep:
            raise HTTPException(status_code=404, detail="no report built yet — use ?build=true for a live preview")
    if format == "html":
        return HTMLResponse(rep["html"])
    return _ser(rep)
