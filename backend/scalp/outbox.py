"""Phase A — transactional-outbox for critical event durability.

MongoDB here is standalone (no multi-document transactions), so the classic
outbox is approximated with the strongest available guarantees:

  1. the producer AWAITS an idempotent outbox insert (unique outbox_key)
     BEFORE/alongside the state change it describes,
  2. an immediate best-effort publish keeps latency low,
  3. the reconcile-loop relay retries every pending row until published —
     a crash between insert and publish only delays the event.

Consumers are idempotent: trade_events publication upserts on event_id.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

TOPIC_TRADE_EVENT = "trade_event"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_outbox_indexes(db) -> None:
    await db.outbox.create_index("outbox_key", unique=True)
    await db.outbox.create_index([("state", 1), ("created_at", 1)])


async def enqueue(db, topic: str, key: str, payload: dict,
                  publish_now: bool = True) -> None:
    """Durable, idempotent producer write (awaited by callers)."""
    await db.outbox.update_one(
        {"outbox_key": key},
        {"$setOnInsert": {"outbox_key": key, "topic": topic,
                          "payload": dict(payload), "state": "pending",
                          "attempts": 0, "created_at": _now()}},
        upsert=True)
    if publish_now:
        try:                       # latency optimisation only — the relay
            await relay_once(db, only_key=key)   # loop is the guarantee
        except Exception:  # noqa: BLE001
            logger.exception("outbox immediate publish failed key=%s", key)


async def _publish(db, doc: dict) -> bool:
    if doc["topic"] == TOPIC_TRADE_EVENT:
        ev = doc["payload"]
        await db.trade_events.update_one(
            {"event_id": ev["event_id"]},
            {"$setOnInsert": dict(ev)}, upsert=True)
        return True
    logger.warning("outbox: unknown topic %s key=%s", doc.get("topic"),
                   doc.get("outbox_key"))
    return False


async def relay_once(db, limit: int = 200, only_key: str | None = None) -> dict:
    """Publish pending outbox rows; failures stay pending with attempts++."""
    q: dict = {"state": "pending"}
    if only_key:
        q["outbox_key"] = only_key
    published = failed = 0
    async for doc in db.outbox.find(q).sort("created_at", 1).limit(limit):
        try:
            ok = await _publish(db, doc)
        except Exception:  # noqa: BLE001
            logger.exception("outbox publish failed key=%s",
                             doc.get("outbox_key"))
            ok = False
        if ok:
            await db.outbox.update_one(
                {"outbox_key": doc["outbox_key"]},
                {"$set": {"state": "published", "published_at": _now()}})
            published += 1
        else:
            await db.outbox.update_one(
                {"outbox_key": doc["outbox_key"]},
                {"$inc": {"attempts": 1},
                 "$set": {"last_attempt_at": _now()}})
            failed += 1
    return {"published": published, "failed": failed}
