"""Health repairs — the state repairs that used to run inside
GET /bot/health-score (release review P1-4). GET endpoints are now
read-only; these idempotent repairs run from the analytics worker on a
schedule and every mutation is written to an immutable `repair_ledger`
row with the affected record ids and a correlation id."""
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


def _hb_age(now: datetime, hb) -> float:
    if not hb:
        return float("inf")
    try:
        return (now - datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
                ).total_seconds()
    except Exception:  # noqa: BLE001 — unparseable = unknown = infinitely old
        return float("inf")


async def _ledger(db, corr: str, user_id: str, kind: str, ids: list,
                  detail: dict | None = None) -> None:
    if not ids:
        return
    await db.repair_ledger.insert_one({
        "correlation_id": corr, "user_id": user_id, "kind": kind,
        "affected_ids": [str(i) for i in ids], "count": len(ids),
        "detail": detail or {}, "source": "health_repairs",
        "at": datetime.now(timezone.utc).isoformat()})


async def run_health_repairs(db, user_id: str) -> dict:
    """Idempotent. Returns counts per repair kind + correlation id."""
    now = datetime.now(timezone.utc)
    corr = f"repair_{uuid.uuid4().hex[:12]}"
    out = {"correlation_id": corr}

    accs = await db.accounts.find(
        {"user_id": user_id, "status": {"$ne": "deleted"}},
        {"label": 1, "status": 1, "dormant": 1, "last_heartbeat": 1}
    ).to_list(length=200)

    # 1 · revive dormant accounts that are back online
    revive = [a["_id"] for a in accs if a.get("dormant")
              and a.get("status") == "connected"
              and _hb_age(now, a.get("last_heartbeat")) < REVIVE_WITHIN_SEC]
    if revive:
        await db.accounts.update_many(
            {"_id": {"$in": revive}},
            {"$set": {"dormant": False, "revived_at": now.isoformat()}})
        await _ledger(db, corr, user_id, "account_revived", revive)
    out["revived"] = len(revive)

    # 2 · mark connected-but-silent (>1h) accounts dormant/disconnected
    dormant = [a["_id"] for a in accs if a.get("status") == "connected"
               and not a.get("dormant")
               and _hb_age(now, a.get("last_heartbeat")) > DORMANT_AFTER_SEC]
    if dormant:
        await db.accounts.update_many(
            {"_id": {"$in": dormant}},
            {"$set": {"status": "disconnected", "dormant": True,
                      "dormant_since": now.isoformat()}})
        await _ledger(db, corr, user_id, "account_dormant", dormant)
    out["dormant"] = len(dormant)

    # 3 · expire pending modifications the EA never acknowledged (>10 min)
    stale_iso = (now - timedelta(minutes=PENDING_MOD_STALE_MIN)).isoformat()
    q = {"user_id": user_id, "status": "open",
         "pending_modification": {"$ne": None},
         "$or": [{"pending_modification.requested_at": {"$lt": stale_iso}},
                 {"pending_modification.requested_at": {"$exists": False}}]}
    ids = [t["_id"] async for t in db.trades.find(q, {"_id": 1})]
    if ids:
        await db.trades.update_many(
            {"_id": {"$in": ids}},
            {"$set": {"pending_modification": None,
                      "pending_modification_expired": True,
                      "pending_modification_expired_at": now.isoformat()}})
        await _ledger(db, corr, user_id, "pending_modification_expired", ids)
    out["pending_expired"] = len(ids)

    # 4 · acknowledge ghost closes older than 24h (history sweep can't fill)
    day_iso = (now - timedelta(hours=GHOST_ACK_AFTER_H)).isoformat()
    q = {"user_id": user_id, "status": "closed", "exit_price": None,
         "ghost_acknowledged": {"$ne": True}, "closed_at": {"$lt": day_iso}}
    ids = [t["_id"] async for t in db.trades.find(q, {"_id": 1})]
    if ids:
        await db.trades.update_many(
            {"_id": {"$in": ids}},
            {"$set": {"ghost_acknowledged": True,
                      "ghost_auto_ack_reason": "older_than_24h",
                      "ghost_acknowledged_at": now.isoformat()}})
        await _ledger(db, corr, user_id, "ghost_ack_old", ids)
    out["ghost_old"] = len(ids)

    # 5 · acknowledge ghosts whose account no longer exists
    cands = await db.trades.find(
        {"user_id": user_id, "status": "closed", "exit_price": None,
         "ghost_acknowledged": {"$ne": True}},
        {"_id": 1, "account_id": 1}).to_list(length=200)
    orphan: list = []
    if cands:
        oids = []
        for aid in {t.get("account_id") for t in cands if t.get("account_id")}:
            try:
                oids.append(ObjectId(aid))
            except (InvalidId, TypeError):
                pass
        live = {str(d["_id"]) for d in await db.accounts.find(
            {"_id": {"$in": oids}}, {"_id": 1}).to_list(length=len(oids))} \
            if oids else set()
        orphan = [t["_id"] for t in cands
                  if t.get("account_id") and t["account_id"] not in live]
        if orphan:
            await db.trades.update_many(
                {"_id": {"$in": orphan}},
                {"$set": {"ghost_acknowledged": True,
                          "ghost_auto_ack_reason": "account_deleted",
                          "ghost_acknowledged_at": now.isoformat()}})
            await _ledger(db, corr, user_id, "ghost_ack_orphan", orphan)
    out["ghost_orphan"] = len(orphan)
    return out


async def run_all_users(db) -> int:
    n = 0
    for uid in await db.accounts.distinct("user_id"):
        if not uid:
            continue
        try:
            await run_health_repairs(db, uid)
            n += 1
        except Exception as e:  # noqa: BLE001 — one user must not stop the sweep
            logger.warning("health repairs failed for user %s: %s", uid, e)
    return n
