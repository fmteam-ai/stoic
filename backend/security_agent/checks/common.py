"""Check helpers shared by every area module. Each check: `async def cXX(db, cfg) -> list[dict]`.
Findings are built with `f(...)`; the runner handles dedup, auto-resolve and failures."""
from datetime import datetime, timedelta, timezone

from security_agent.findings import build
from security_agent.playbooks import playbook


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso_ago(**kw) -> str:
    return (now() - timedelta(**kw)).isoformat()


def th(cfg: dict, check: str, key: str, default):
    return (cfg.get("thresholds") or {}).get(check, {}).get(key, default)


def f(check_id: str, key: str, severity: str, area: str, what: str, evidence: dict | None = None, title: str | None = None) -> dict:
    pb = playbook(check_id)
    return build(check_id, f"{check_id}:{key}", severity=severity, area=area, title=title or pb["title"],
                 what_happened=what, why_it_matters=pb["why_it_matters"], evidence=evidence,
                 solution=pb["solution"], verify=pb["verify"])


async def window_counts(db, scope_prefix: str, minutes: int) -> list[dict]:
    """rate_limits docs are `_id = scope:identifier[:more]:window_start` with `n` — sum per identifier
    over the recent windows (the counters already exist for every auth/bridge failure path)."""
    cutoff = now() - timedelta(minutes=minutes)
    out: dict[str, int] = {}
    async for d in db.rate_limits.find({"_id": {"$regex": f"^{scope_prefix}:"}, "expires_at": {"$gte": cutoff}}).limit(5000):
        parts = str(d["_id"]).split(":")
        ident = ":".join(parts[1:-1])
        out[ident] = out.get(ident, 0) + int(d.get("n") or 0)
    return [{"identifier": k, "n": v} for k, v in out.items()]


async def events(db, kind: str, minutes: int = 10, mark: bool = True) -> list[dict]:
    rows = await db.security_events.find({"kind": kind, "at": {"$gte": iso_ago(minutes=minutes)}}).limit(2000).to_list(length=2000)
    if mark and rows:
        await db.security_events.update_many({"_id": {"$in": [r["_id"] for r in rows]}}, {"$set": {"processed": True}})
    return rows
