"""Security & Health Agent — admin API (SA1: read-only; SA3/SA4 add writes with step-up)."""
from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from auth import get_current_user, require_admin
from database import get_db
from security_agent import config
from security_agent.checks import CHECKS

router = APIRouter(prefix="/admin/security", tags=["security-agent"])


def _ser(d: dict) -> dict:
    d = dict(d)
    d["id"] = str(d.pop("_id"))
    d.pop("expires_at", None)
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
    return _ser(row)


@router.get("/check-runs")
async def check_runs(user=Depends(get_current_user)):
    require_admin(user)
    db = get_db()
    latest = {r["check_id"]: r async for r in db.security_check_runs.find({"_id": {"$regex": "^latest:"}})}
    return {"checks": [{"check_id": cid, "interval_s": iv, **{k: v for k, v in (latest.get(cid) or {}).items() if k != "_id"}}
                       for cid, (_, iv) in CHECKS.items()]}
