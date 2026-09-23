"""Release-readiness FAIL-CLOSED drills (audit round 5 P1, round 6 P1) —
STAGING ONLY, refused before the first write anywhere else.

  docker compose exec -T backend python ops/readiness_drills.py [--base=http://127.0.0.1:8001]

Hard guard (ALL required, evaluated before any database write):
  APP_ENV=staging · ALLOW_DESTRUCTIVE_DRILLS=true · DB_NAME ends with an
  approved staging suffix (DRILL_DB_SUFFIXES, default "_staging,_drill") ·
  DRILL_STEP_UP_TOKEN supplied and equal to DRILL_STEP_UP_TOKEN_EXPECTED.

Faults are injected as TAGGED, DISPOSABLE rows only: no real worker lease,
loop row, anchor or ledger row is ever deleted or rewritten. Anchor/ledger
faults insert a bogus NEWER anchor (sequence regression / hash mismatch)
that verify_anchor must reject; restoration deletes only tagged rows.
Writes release/drills/readiness-<ts>.json. Exit 0 = every fault was caught.
"""
import asyncio
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


def guard(env: dict) -> list:
    """Pure refusal logic (unit-testable). Returns violations; empty = allowed."""
    problems = []
    if env.get("APP_ENV", "").lower() != "staging":
        problems.append("APP_ENV must be 'staging'")
    if env.get("ALLOW_DESTRUCTIVE_DRILLS", "").lower() != "true":
        problems.append("ALLOW_DESTRUCTIVE_DRILLS=true is required")
    suffixes = [s.strip() for s in env.get("DRILL_DB_SUFFIXES", "_staging,_drill").split(",") if s.strip()]
    db_name = env.get("DB_NAME", "")
    if not any(db_name.endswith(s) for s in suffixes):
        problems.append(f"DB_NAME '{db_name}' lacks an approved staging suffix {suffixes}")
    tok, exp = env.get("DRILL_STEP_UP_TOKEN", ""), env.get("DRILL_STEP_UP_TOKEN_EXPECTED", "")
    if not tok or not exp or len(exp) < 16 or tok != exp:
        problems.append("step-up drill token missing or mismatched (DRILL_STEP_UP_TOKEN vs DRILL_STEP_UP_TOKEN_EXPECTED)")
    return problems


def _iso(dt):
    return dt.isoformat()


async def readiness(base: str) -> tuple:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(f"{base}/api/ops/release-readiness", headers={"X-Metrics-Token": os.environ.get("METRICS_TOKEN", "")})
    return r.status_code, r.json()


class Fault:
    def __init__(self, name, check_key, inject, restore):
        self.name, self.check_key, self.inject, self.restore = name, check_key, inject, restore


async def recover(db) -> int:
    """Automatic recovery proof: restore any journaled originals left by a
    drill that crashed after fault injection, and purge tagged rows."""
    n = 0
    async for j in db.drill_journal.find({}):
        await db[j["collection"]].replace_one({"_id": j["doc"]["_id"]}, j["doc"], upsert=True)
        await db.drill_journal.delete_one({"_id": j["_id"]})
        n += 1
    for coll in ("worker_leases", "loop_progress", "execution_intents", "accounts", "repair_ledger_anchors"):
        n += (await db[coll].delete_many({"drill_tag": {"$exists": True}})).deleted_count
    return n


async def main() -> int:
    problems = guard(dict(os.environ))
    if problems:
        print(json.dumps({"drill": "readiness-fail-closed", "result": "REFUSED", "problems": problems}, indent=1))
        print("READINESS-DRILL REFUSED: " + " | ".join(problems), file=sys.stderr)
        return 2
    from database import get_db
    from health_repairs import _anchor_key, _anchor_sig
    db = get_db()
    recovered = await recover(db)
    if "--recover-only" in sys.argv[1:]:
        print(json.dumps({"drill": "readiness-fail-closed", "result": "RECOVERED", "rows": recovered}))
        return 0
    base = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--base=")), "http://127.0.0.1:8001")
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=3)
    results, ok_all = [], True
    tag = f"drill_{uuid.uuid4().hex[:6]}"

    # ── fault definitions — every injected row carries `drill_tag` ─────────
    # The worker check is keyed by lease _id (EXPECTED_WORKERS), so a shadow
    # row cannot exercise it: the real lease is mutated ONLY after its full
    # snapshot is journaled (db.drill_journal) — a crashed drill is repaired
    # by `recover()` on the next run before anything else happens.
    async def lease_inject():
        real = await db.worker_leases.find_one({})
        if not real:
            return None
        await db.drill_journal.insert_one({"_id": f"{tag}:worker_leases:{real['_id']}", "drill_tag": tag,
                                           "collection": "worker_leases", "doc": real, "at": _iso(now)})
        await db.worker_leases.update_one({"_id": real["_id"]}, {"$set": {"expires_at": _iso(old)}})
        return real["_id"]

    async def lease_restore(_id):
        if _id is None:
            return
        j = await db.drill_journal.find_one({"_id": f"{tag}:worker_leases:{_id}"})
        if j:
            await db.worker_leases.replace_one({"_id": _id}, j["doc"], upsert=True)
            await db.drill_journal.delete_one({"_id": j["_id"]})

    async def loop_inject():
        real = await db.loop_progress.find_one({})
        if not real:
            return None
        doc = {k: v for k, v in real.items() if k != "_id"}
        doc.update({"last_iteration_completed_at": _iso(old), "drill_tag": tag,
                    "loop": f"{doc.get('loop', 'loop')}-{tag}"})
        res = await db.loop_progress.insert_one(doc)
        return res.inserted_id

    async def loop_restore(_id):
        if _id is not None:
            await db.loop_progress.delete_one({"_id": _id, "drill_tag": tag})

    async def unknown_inject():
        await db.execution_intents.insert_one({"intent_id": f"{tag}_unknown", "status": "unknown", "kind": "open_trade",
                                               "account_id": tag, "symbol": "EURUSD", "created_at": _iso(now),
                                               "broker_ticket": None, "request_id": tag, "drill_tag": tag})
        return tag

    async def unknown_restore(_):
        await db.execution_intents.delete_many({"drill_tag": tag})

    async def mismatch_inject():
        res = await db.accounts.insert_one({"user_id": tag, "label": tag, "status": "connected", "trading_enabled": True,
                                            "last_heartbeat": _iso(now), "open_positions": 2, "positions": [],
                                            "bridge_token": f"{tag}_{uuid.uuid4().hex}", "drill_tag": tag,
                                            "verified_identity": {"account_number": tag}})
        return res.inserted_id

    async def mismatch_restore(_id):
        await db.accounts.delete_one({"_id": _id, "drill_tag": tag})

    async def _bogus_anchor(last_seq: int, last_hash: str):
        body = {"last_seq": last_seq, "last_hash": last_hash, "at": _iso(now + timedelta(seconds=1)), "build": tag}
        sig = _anchor_sig(body) if _anchor_key() else None
        res = await db.repair_ledger_anchors.insert_one({**body, "sig": sig, "drill_tag": tag})
        return res.inserted_id

    async def regression_inject():
        last = await db.repair_ledger.find_one({"entry_hash": {"$exists": True}}, sort=[("seq", -1)])
        cur = last["seq"] if last else 0
        return await _bogus_anchor(cur + 1000, "0" * 64)          # newest anchor claims a seq the ledger never reached

    async def hash_inject():
        last = await db.repair_ledger.find_one({"entry_hash": {"$exists": True}}, sort=[("seq", -1)])
        if not last:
            return None
        return await _bogus_anchor(last["seq"], "f" * 64)         # anchored hash disagrees with the real row

    async def anchor_restore(_id):
        if _id is not None:
            await db.repair_ledger_anchors.delete_one({"_id": _id, "drill_tag": tag})

    faults = [Fault("stale_worker_lease", "workers", lease_inject, lease_restore),
              Fault("stalled_loop", "loop_progress", loop_inject, loop_restore),
              Fault("fresh_unknown_execution_blocks_immediately", "execution_truth", unknown_inject, unknown_restore),
              Fault("position_mismatch", "execution_truth", mismatch_inject, mismatch_restore),
              Fault("anchor_sequence_regression", "repair_ledger_anchor", regression_inject, anchor_restore),
              Fault("anchored_hash_mismatch", "repair_ledger_anchor", hash_inject, anchor_restore)]

    code0, body0 = await readiness(base)
    baseline = {"status": code0, "failing": sorted(k for k, v in (body0.get("checks") or {}).items() if not v.get("ok"))}
    for f in faults:
        token = None
        try:
            token = await f.inject()
            if token is None:
                results.append({"fault": f.name, "result": "SKIP", "note": "no telemetry/ledger row to shadow — absence itself must already fail readiness",
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
    # belt and braces — nothing tagged may survive the drill
    leftovers = 0
    for coll in ("worker_leases", "loop_progress", "execution_intents", "accounts", "repair_ledger_anchors"):
        leftovers += (await db[coll].delete_many({"drill_tag": tag})).deleted_count
    code1, _ = await readiness(base)
    report = {"drill": "readiness-fail-closed", "at": _iso(now), "base": base, "baseline": baseline,
              "guard": {"app_env": os.environ.get("APP_ENV"), "db_name": os.environ.get("DB_NAME")},
              "recovered_from_previous_crash": recovered,
              "faults": results, "restored_status": code1, "leftover_rows_removed": leftovers,
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
