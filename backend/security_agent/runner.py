"""Security & Health Agent — scheduler: runs due checks, records runs, isolates failures."""
import asyncio
import logging
import time
import traceback
from datetime import datetime, timedelta, timezone

from security_agent import actions, alerts, config, reports
from security_agent.checks import BOOT_CHECKS, CHECKS
from security_agent.findings import auto_resolve_cleared, build, ensure_indexes, open_or_update
from security_agent.playbooks import playbook
from security_agent.redact import mask

log = logging.getLogger("security_agent")
_last_run: dict[str, float] = {}


def due(check_id: str, interval: int, *, now_ts: float | None = None, boot: bool = False) -> bool:
    now_ts = time.time() if now_ts is None else now_ts
    last = _last_run.get(check_id)
    if last is None:
        return boot or interval <= 300 or check_id in BOOT_CHECKS   # long-interval checks wait for their slot after boot
    return now_ts - last >= interval


async def run_check(db, check_id: str, fn, cfg: dict) -> dict:
    t0 = time.perf_counter()
    status, n_found, created = "ok", 0, 0
    try:
        found = await asyncio.wait_for(fn(db, cfg), timeout=60)
        keys = set()
        for fd in found:
            keys.add(fd["dedup_key"])
            res = await open_or_update(db, fd, retention_days=int(cfg.get("retention_days") or 180))
            created += int(res["created"])
        n_found = len(found)
        await auto_resolve_cleared(db, check_id, keys)
    except Exception as e:  # noqa: BLE001 — a failing check is itself a finding, never a crash
        status = "failed"
        pb = playbook("agent_check_failed")
        await open_or_update(db, build("agent_check_failed", f"agent_check_failed:{check_id}", severity="medium", area="platform",
                                       title=f"{pb['title']}: {check_id}", what_happened=f"{check_id} raised {type(e).__name__}: {mask(str(e))[:300]}",
                                       why_it_matters=pb["why_it_matters"], evidence={"traceback": traceback.format_exc()[-1500:]},
                                       solution=pb["solution"], verify=pb["verify"]))
        log.warning("security check %s failed: %s", check_id, e)
    else:
        await auto_resolve_cleared(db, "agent_check_failed", {f"agent_check_failed:{c}" for c in CHECKS if c != check_id} | set())
    ms = round((time.perf_counter() - t0) * 1000, 1)
    _last_run[check_id] = time.time()
    run = {"check_id": check_id, "at": datetime.now(timezone.utc).isoformat(), "duration_ms": ms, "status": status,
           "findings": n_found, "new_findings": created, "expires_at": datetime.now(timezone.utc) + timedelta(days=7)}
    await db.security_check_runs.insert_one(dict(run))
    await db.security_check_runs.update_one({"_id": f"latest:{check_id}"}, {"$set": {k: v for k, v in run.items() if k != "expires_at"}}, upsert=True)
    return run


async def tick(db, *, boot: bool = False) -> list[dict]:
    cfg = await config.load(db)
    runs = []
    for cid, (fn, interval) in CHECKS.items():
        if due(cid, interval, boot=boot):
            runs.append(await run_check(db, cid, fn, cfg))
    for name, step in (("actions", actions.sweep), ("alerts", alerts.sweep), ("reports", reports.scheduler_tick)):
        try:
            await asyncio.wait_for(step(db, cfg), timeout=60)
        except Exception as e:  # noqa: BLE001 — SA3 stages are isolated from detection and from each other
            log.warning("security agent %s stage failed: %s", name, type(e).__name__)
    return runs


async def loop(interval_s: int = 60) -> None:
    from database import get_db
    db = get_db()
    await ensure_indexes(db)
    first = True
    while True:
        try:
            runs = await tick(db, boot=first)
            first = False
            if runs:
                log.info("security agent tick: %d check(s) ran, %d finding(s)", len(runs), sum(r["findings"] for r in runs))
        except Exception as e:  # noqa: BLE001
            log.exception("security agent tick failed: %s", e)
        await asyncio.sleep(interval_s)
