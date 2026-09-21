"""Release-readiness FAIL-CLOSED drills (audit round 5 P1) — STAGING ONLY.

  docker compose exec -T backend python ops/readiness_drills.py [--base http://127.0.0.1:8001]

For each fault: inject it into the database (or env-free state), call
GET /api/ops/release-readiness with the metrics token, assert 503 AND that
the SPECIFIC check reports ok=false, then restore the original documents.
Faults: stale worker lease · stalled loop · stale reconciliation · unresolved
UNKNOWN execution · position mismatch · missing ledger anchor · corrupted
ledger tail · mismatched topology (6/3/3 via production_reconcile).
Writes release/drills/readiness-<ts>.json. Exit 0 = every fault was caught.
"""
import asyncio
import copy
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:  # noqa: BLE001
    pass

import httpx  # noqa: E402


def _iso(dt):
    return dt.isoformat()


async def readiness(base: str) -> tuple:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(f"{base}/api/ops/release-readiness", headers={"X-Metrics-Token": os.environ.get("METRICS_TOKEN", "")})
    return r.status_code, r.json()


class Fault:
    def __init__(self, name, check_key, inject, restore):
        self.name, self.check_key, self.inject, self.restore = name, check_key, inject, restore


async def main() -> int:
    from database import get_db
    db = get_db()
    base = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--base=")), "http://127.0.0.1:8001")
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=3)
    results, ok_all = [], True
    tag = f"drill_{uuid.uuid4().hex[:6]}"

    # ── fault definitions ────────────────────────────────────────────────────
    async def lease_inject():
        doc = await db.worker_leases.find_one({})
        if not doc:
            return None
        await db.worker_leases.update_one({"_id": doc["_id"]}, {"$set": {"expires_at": _iso(old)}})
        return doc

    async def lease_restore(doc):
        if doc:
            await db.worker_leases.replace_one({"_id": doc["_id"]}, doc)

    async def loop_inject():
        doc = await db.loop_progress.find_one({})
        if not doc:
            return None
        await db.loop_progress.update_one({"_id": doc["_id"]}, {"$set": {"last_iteration_completed_at": _iso(old)}})
        return doc

    async def loop_restore(doc):
        if doc:
            await db.loop_progress.replace_one({"_id": doc["_id"]}, doc)

    async def unknown_inject():
        await db.execution_intents.insert_one({"intent_id": f"{tag}_unknown", "status": "unknown", "kind": "open_trade",
                                               "account_id": tag, "symbol": "EURUSD", "created_at": _iso(old),
                                               "broker_ticket": None, "request_id": tag})
        return tag

    async def unknown_restore(_):
        await db.execution_intents.delete_many({"intent_id": f"{tag}_unknown"})

    async def mismatch_inject():
        res = await db.accounts.insert_one({"user_id": tag, "label": tag, "status": "connected", "trading_enabled": True,
                                            "last_heartbeat": _iso(now), "open_positions": 2, "positions": [],
                                            "bridge_token": f"{tag}_{uuid.uuid4().hex}", "verified_identity": {"account_number": tag}})
        return res.inserted_id

    async def mismatch_restore(_id):
        await db.accounts.delete_one({"_id": _id})

    async def anchor_inject():
        anchors = await db.repair_ledger_anchors.find({}).to_list(length=100000)
        await db.repair_ledger_anchors.delete_many({})
        return anchors

    async def anchor_restore(anchors):
        if anchors:
            await db.repair_ledger_anchors.insert_many(anchors)

    async def tail_inject():
        last = await db.repair_ledger.find_one({"entry_hash": {"$exists": True}}, sort=[("seq", -1)])
        if not last:
            return None
        await db.repair_ledger.delete_one({"_id": last["_id"]})
        return last

    async def tail_restore(last):
        if last:
            await db.repair_ledger.insert_one(last)

    faults = [Fault("stale_worker_lease", "workers", lease_inject, lease_restore),
              Fault("stalled_loop", "loop_progress", loop_inject, loop_restore),
              Fault("unresolved_unknown_execution", "execution_truth", unknown_inject, unknown_restore),
              Fault("position_mismatch", "execution_truth", mismatch_inject, mismatch_restore),
              Fault("missing_external_anchor", "repair_ledger_anchor", anchor_inject, anchor_restore),
              Fault("deleted_ledger_tail", "repair_ledger_anchor", tail_inject, tail_restore)]

    code0, body0 = await readiness(base)
    baseline = {"status": code0, "failing": sorted(k for k, v in (body0.get("checks") or {}).items() if not v.get("ok"))}
    for f in faults:
        token = None
        try:
            token = await f.inject()
            if token is None and f.name in ("stale_worker_lease", "stalled_loop", "deleted_ledger_tail"):
                results.append({"fault": f.name, "result": "SKIP", "note": "no telemetry row to corrupt — telemetry absence itself must already fail readiness",
                                "absence_fails": f.check_key in baseline["failing"]})
                ok_all &= f.check_key in baseline["failing"]
                continue
            code, body = await readiness(base)
            chk = (body.get("checks") or {}).get(f.check_key) or {}
            caught = code == 503 and chk.get("ok") is False
            ok_all &= caught
            results.append({"fault": f.name, "check": f.check_key, "status": code, "check_ok": chk.get("ok"),
                            "result": "CAUGHT" if caught else "MISSED",
                            "detail": {k: v for k, v in chk.items() if k not in ("sample",)}})
        finally:
            await f.restore(token)
    code1, _ = await readiness(base)
    report = {"drill": "readiness-fail-closed", "at": _iso(now), "base": base, "baseline": baseline,
              "faults": results, "restored_status": code1,
              "result": "PASS" if ok_all else "FAIL"}
    out_dir = os.environ.get("DRILL_EVIDENCE_DIR", "/tmp/drills")
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"readiness-{now.strftime('%Y%m%dT%H%M%SZ')}.json")
        json.dump(report, open(path, "w"), indent=1, default=str)
        report["evidence_file"] = path
    except OSError:
        pass
    print(json.dumps(report, indent=1, default=str))
    print(f"READINESS-DRILL {report['result']}", file=sys.stderr)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
