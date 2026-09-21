"""Health repairs — the state repairs that used to run inside
GET /bot/health-score (release review P1-4). GET endpoints are read-only;
these idempotent repairs run from the analytics worker on a schedule.

Audit P1-3 (atomic repair + audit record) — outbox state machine:
  1. a PENDING ledger row is written FIRST (unique on correlation_id+kind+
     batch_hash; hash-chained, carrying the affected ids and a `before`
     summary),
  2. the mutation is conditional (`repair_id` stamped on every record it
     touches) and idempotent,
  3. the ledger row is marked COMPLETED with an `after` summary.
A crash between 1 and 3 leaves a PENDING row; `replay_incomplete()` runs at
the start of every sweep and finishes it exactly once.

Audit P1-4 — this is an APPEND-ONLY APPLICATION LEDGER: rows are
sha256-chained (`seq`, `prev_hash`, `entry_hash` over the immutable fields);
`verify_repair_chain()` detects any edit/removal; `guard_ledger_mutation()`
raises + alerts if application code ever tries to update/delete rows other
than the PENDING→COMPLETED transition. DB-level WORM is out of app scope.
"""
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId

logger = logging.getLogger("health.repairs")

DORMANT_AFTER_SEC = 3600
REVIVE_WITHIN_SEC = 300
PENDING_MOD_STALE_MIN = 10
GHOST_ACK_AFTER_H = 24
GENESIS = "0" * 64
CHAINED_FIELDS = ("seq", "prev_hash", "correlation_id", "kind", "user_id",
                  "affected_ids", "batch_hash", "count", "before", "source", "at")
COMPLETION_FIELDS = {"state", "completed_at", "after", "replayed", "replayed_at"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hb_age(now: datetime, hb) -> float:
    if not hb:
        return float("inf")
    try:
        return (now - datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
                ).total_seconds()
    except Exception:  # noqa: BLE001 — unparseable = unknown = infinitely old
        return float("inf")


def _entry_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str,
                                     separators=(",", ":")).encode()).hexdigest()


def batch_hash(ids: list) -> str:
    return hashlib.sha256("\n".join(sorted(str(i) for i in ids)).encode()).hexdigest()[:24]


async def ensure_ledger_indexes(db) -> None:
    await db.repair_ledger.create_index(
        [("correlation_id", 1), ("kind", 1), ("batch_hash", 1)], unique=True,
        name="uniq_repair_batch", partialFilterExpression={"batch_hash": {"$exists": True}})
    await db.repair_ledger.create_index([("seq", 1)], name="seq", unique=True,
                                        partialFilterExpression={"seq": {"$exists": True}})
    await db.repair_ledger.create_index([("state", 1), ("at", 1)], name="state_at")


class LedgerMutationRefused(RuntimeError):
    """Raised when application code tries to edit/delete ledger history."""


async def guard_ledger_mutation(db, op: str, actor: str = "app") -> None:
    """Application-layer WORM guard (P1-4): the ONLY allowed write after
    insert is the PENDING→COMPLETED transition done by this module. Any
    other update/delete attempt is refused AND recorded as a critical
    ops alert so operators see tampering attempts."""
    await db.ops_alerts.insert_one({
        "severity": "critical", "kind": "repair_ledger_mutation_refused",
        "message": f"refused {op} on repair_ledger by {actor}",
        "at": _now().isoformat(), "acknowledged": False, "synthetic": False})
    raise LedgerMutationRefused(f"repair_ledger is append-only — {op} refused")


async def _open_pending(db, corr: str, user_id: str, kind: str, ids: list,
                        before: dict | None) -> dict | None:
    """Step 1 — chained PENDING row, written BEFORE the mutation. Returns
    None when the identical batch was already ledgered (idempotent)."""
    bh = batch_hash(ids)
    if await db.repair_ledger.find_one({"correlation_id": corr, "kind": kind, "batch_hash": bh}, {"_id": 1}):
        return None
    last = await db.repair_ledger.find_one({"entry_hash": {"$exists": True}}, sort=[("seq", -1)])
    doc = {"seq": (last["seq"] + 1) if last else 1,
           "prev_hash": last["entry_hash"] if last else GENESIS,
           "correlation_id": corr, "kind": kind, "user_id": user_id,
           "affected_ids": [str(i) for i in ids], "batch_hash": bh, "count": len(ids),
           "before": before or {}, "source": "health_repairs", "at": _now().isoformat()}
    doc["entry_hash"] = _entry_hash({k: doc[k] for k in CHAINED_FIELDS})
    doc["state"] = "pending"
    doc["repair_id"] = f"{corr}:{kind}:{bh}"
    try:
        await db.repair_ledger.insert_one(doc)
    except Exception as e:  # noqa: BLE001 — duplicate seq/batch under concurrency → retry once
        if "duplicate" not in str(e).lower():
            raise
        return await _open_pending(db, corr, user_id, kind, ids, before)
    return doc


async def _complete(db, row: dict, after: dict, replayed: bool = False) -> None:
    upd = {"state": "completed", "completed_at": _now().isoformat(), "after": after}
    if replayed:
        upd.update(replayed=True, replayed_at=upd["completed_at"])
    await db.repair_ledger.update_one({"_id": row["_id"], "state": "pending"}, {"$set": upd})


# ── the repairs: (collection, filter-builder, $set) keyed by kind ────────────
def _mutation(kind: str, row: dict, now_iso: str) -> tuple:
    """Conditional, idempotent mutation for a ledgered batch. The filter
    re-checks the precondition so a replay after a crash cannot re-apply
    a repair to records that moved on; the update stamps `repair_id`."""
    rid = row["repair_id"]
    oids = []
    for i in row["affected_ids"]:
        try:
            oids.append(ObjectId(i))
        except (InvalidId, TypeError):
            pass
    base = {"_id": {"$in": oids}, "repair_ids": {"$ne": rid}}
    if kind == "account_revived":
        return "accounts", {**base, "dormant": True}, {"$set": {"dormant": False, "revived_at": now_iso}}
    if kind == "account_dormant":
        return "accounts", {**base, "status": "connected"}, {"$set": {
            "status": "disconnected", "dormant": True, "dormant_since": now_iso}}
    if kind == "pending_modification_expired":
        return "trades", {**base, "pending_modification": {"$ne": None}}, {"$set": {
            "pending_modification": None, "pending_modification_expired": True,
            "pending_modification_expired_at": now_iso}}
    if kind == "ghost_ack_old":
        return "trades", {**base, "ghost_acknowledged": {"$ne": True}}, {"$set": {
            "ghost_acknowledged": True, "ghost_auto_ack_reason": "older_than_24h",
            "ghost_acknowledged_at": now_iso}}
    if kind == "ghost_ack_orphan":
        return "trades", {**base, "ghost_acknowledged": {"$ne": True}}, {"$set": {
            "ghost_acknowledged": True, "ghost_auto_ack_reason": "account_deleted",
            "ghost_acknowledged_at": now_iso}}
    raise ValueError(f"unknown repair kind {kind}")


async def _apply(db, row: dict, replayed: bool = False) -> int:
    """Steps 2+3 — conditional mutation stamped with repair_id, then COMPLETED."""
    now_iso = _now().isoformat()
    col, flt, upd = _mutation(row["kind"], row, now_iso)
    upd = {**upd, "$addToSet": {"repair_ids": row["repair_id"]}}
    res = await db[col].update_many(flt, upd)
    stamped = await db[col].count_documents({"repair_ids": row["repair_id"]})
    await _complete(db, row, {"modified": res.modified_count, "stamped": stamped,
                              "applied_at": now_iso}, replayed=replayed)
    return res.modified_count


async def _repair(db, corr: str, user_id: str, kind: str, ids: list, before: dict) -> int:
    if not ids:
        return 0
    row = await _open_pending(db, corr, user_id, kind, ids, before)
    if row is None:
        return 0
    return await _apply(db, row)


async def replay_incomplete(db, older_than_sec: int = 60) -> int:
    """Recovery: finish PENDING rows whose mutation/finalization never
    completed (crash between steps). Exactly-once by construction: the
    mutation filter excludes records already stamped with the repair_id."""
    cutoff = (_now() - timedelta(seconds=older_than_sec)).isoformat()
    n = 0
    async for row in db.repair_ledger.find({"state": "pending", "at": {"$lt": cutoff}}).sort("seq", 1):
        try:
            await _apply(db, row, replayed=True)
            n += 1
        except Exception as e:  # noqa: BLE001 — one stuck row must not block the sweep
            logger.warning("repair replay failed for %s: %s", row.get("repair_id"), e)
    return n


async def verify_repair_chain(db) -> dict:
    prev, checked, anomalies, legacy = GENESIS, 0, [], 0
    async for e in db.repair_ledger.find({}).sort([("seq", 1), ("at", 1)]):
        if "entry_hash" not in e:
            legacy += 1
            continue
        checked += 1
        if e.get("prev_hash") != prev:
            anomalies.append({"seq": e.get("seq"), "error": "chain_link_broken"})
        if _entry_hash({k: e.get(k) for k in CHAINED_FIELDS}) != e.get("entry_hash"):
            anomalies.append({"seq": e.get("seq"), "error": "entry_hash_mismatch"})
        prev = e.get("entry_hash")
    pending = await db.repair_ledger.count_documents({"state": "pending"})
    return {"ok": not anomalies, "chained_entries": checked, "legacy_unchained_entries": legacy,
            "pending_incomplete": pending, "anomalies": anomalies[:50],
            "ledger_class": "append-only application ledger (sha256 chain); DB-level WORM not enforced",
            "verified_at": _now().isoformat()}


async def run_health_repairs(db, user_id: str) -> dict:
    """Idempotent. Returns counts per repair kind + correlation id."""
    now = _now()
    corr = f"repair_{uuid.uuid4().hex[:12]}"
    out = {"correlation_id": corr}

    accs = await db.accounts.find(
        {"user_id": user_id, "status": {"$ne": "deleted"}},
        {"label": 1, "status": 1, "dormant": 1, "last_heartbeat": 1}
    ).to_list(length=200)

    # 1 · revive dormant accounts that are back online
    revive = [a for a in accs if a.get("dormant") and a.get("status") == "connected"
              and _hb_age(now, a.get("last_heartbeat")) < REVIVE_WITHIN_SEC]
    out["revived"] = await _repair(db, corr, user_id, "account_revived", [a["_id"] for a in revive],
                                   {"dormant": True, "status": "connected"})

    # 2 · mark connected-but-silent (>1h) accounts dormant/disconnected
    dormant = [a for a in accs if a.get("status") == "connected" and not a.get("dormant")
               and _hb_age(now, a.get("last_heartbeat")) > DORMANT_AFTER_SEC]
    out["dormant"] = await _repair(db, corr, user_id, "account_dormant", [a["_id"] for a in dormant],
                                   {"status": "connected", "dormant": False,
                                    "oldest_heartbeat": min((a.get("last_heartbeat") or "" for a in dormant), default=None)})

    # 3 · expire pending modifications the EA never acknowledged (>10 min)
    stale_iso = (now - timedelta(minutes=PENDING_MOD_STALE_MIN)).isoformat()
    q = {"user_id": user_id, "status": "open", "pending_modification": {"$ne": None},
         "$or": [{"pending_modification.requested_at": {"$lt": stale_iso}},
                 {"pending_modification.requested_at": {"$exists": False}}]}
    ids = [t["_id"] async for t in db.trades.find(q, {"_id": 1})]
    out["pending_expired"] = await _repair(db, corr, user_id, "pending_modification_expired", ids,
                                           {"pending_modification": "set", "stale_before": stale_iso})

    # 4 · acknowledge ghost closes older than 24h (history sweep can't fill)
    day_iso = (now - timedelta(hours=GHOST_ACK_AFTER_H)).isoformat()
    q = {"user_id": user_id, "status": "closed", "exit_price": None,
         "ghost_acknowledged": {"$ne": True}, "closed_at": {"$lt": day_iso}}
    ids = [t["_id"] async for t in db.trades.find(q, {"_id": 1})]
    out["ghost_old"] = await _repair(db, corr, user_id, "ghost_ack_old", ids,
                                     {"ghost_acknowledged": False, "closed_before": day_iso})

    # 5 · acknowledge ghosts whose account no longer exists
    cands = await db.trades.find(
        {"user_id": user_id, "status": "closed", "exit_price": None,
         "ghost_acknowledged": {"$ne": True}}, {"_id": 1, "account_id": 1}).to_list(length=200)
    orphan: list = []
    if cands:
        oids = []
        for aid in {t.get("account_id") for t in cands if t.get("account_id")}:
            try:
                oids.append(ObjectId(aid))
            except (InvalidId, TypeError):
                pass
        live = {str(d["_id"]) for d in await db.accounts.find(
            {"_id": {"$in": oids}}, {"_id": 1}).to_list(length=len(oids))} if oids else set()
        orphan = [t["_id"] for t in cands if t.get("account_id") and t["account_id"] not in live]
    out["ghost_orphan"] = await _repair(db, corr, user_id, "ghost_ack_orphan", orphan,
                                        {"ghost_acknowledged": False, "account_exists": False})
    return out


async def eligible_user_ids(db) -> list:
    """P2-3 — accounts ∪ users that still own repair candidates (orphan
    ghosts / stale pending mods) even when no account row remains."""
    uids = set(await db.accounts.distinct("user_id"))
    uids |= set(await db.trades.distinct("user_id", {"status": "closed", "exit_price": None,
                                                     "ghost_acknowledged": {"$ne": True}}))
    uids |= set(await db.trades.distinct("user_id", {"status": "open", "pending_modification": {"$ne": None}}))
    return sorted(u for u in uids if u)


async def run_all_users(db) -> int:
    try:
        await ensure_ledger_indexes(db)
        replayed = await replay_incomplete(db)
        if replayed:
            logger.info("replayed %d incomplete repairs", replayed)
    except Exception as e:  # noqa: BLE001
        logger.warning("repair ledger recovery failed: %s", e)
    n = 0
    for uid in await eligible_user_ids(db):
        try:
            await run_health_repairs(db, uid)
            n += 1
        except Exception as e:  # noqa: BLE001 — one user must not stop the sweep
            logger.warning("health repairs failed for user %s: %s", uid, e)
    return n
