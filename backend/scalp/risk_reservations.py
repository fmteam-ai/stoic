"""review item 2 · Provisional risk reservations.

A reservation is written BEFORE an order is queued and held until the broker
outcome is certain, so replacement orders can never consume the same account
risk while a submission is in-flight or uncertain:

    RISK_RESERVED → QUEUED_UNCONFIRMED → SLOT_LINKED → RELEASED
                                (uncertain=True keeps it held for
                                 reconciliation to resolve)

Release reasons: broker_reject | broker_ack | closed | reconciled | expired.

Round 18 review — timestamps are NATIVE BSON datetimes, every document keeps
an `active` flag, and DB-level unique constraints (ensure_reservation_indexes)
enforce one active reservation per decision and per linked trade.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

ACTIVE_STATES = ("RISK_RESERVED", "QUEUED_UNCONFIRMED", "SLOT_LINKED")
TERMINAL_STATES = ("RELEASED",)
# audit P1 · guarded transition graph — state -> allowed predecessors.
# RELEASED is terminal: once released a reservation can NEVER re-activate.
ALLOWED_PREV = {
    "QUEUED_UNCONFIRMED": ("RISK_RESERVED",),
    "SLOT_LINKED": ("RISK_RESERVED", "QUEUED_UNCONFIRMED"),
    "RELEASED": ACTIVE_STATES,
}
STALE_TTL_SEC = 900


def _now() -> datetime:
    """Native BSON datetime (review item 8) — range queries stay type-safe."""
    return datetime.now(timezone.utc)


async def ensure_reservation_indexes(db) -> None:
    """review item 8 — DB-enforced reservation constraints:
    unique reservation_id, one ACTIVE reservation per decision, one ACTIVE
    reservation per linked trade, plus the sweep/monitor compound index."""
    c = db.risk_reservations
    await c.create_index("reservation_id", unique=True)
    await c.create_index("decision_id", unique=True,
                         partialFilterExpression={"active": True},
                         name="uniq_active_decision")
    await c.create_index("trade_id", unique=True,
                         partialFilterExpression={
                             "active": True,
                             "trade_id": {"$type": "string"}},
                         name="uniq_active_trade")
    await c.create_index([("account_id", 1), ("state", 1),
                          ("updated_at", -1)])


async def reserve(db, *, account_id: str, user_id: str, decision_id: str,
                  risk_usd: float, lot: float) -> dict:
    doc = {"reservation_id": uuid.uuid4().hex, "account_id": account_id,
           "user_id": user_id, "decision_id": decision_id,
           "risk_usd": round(float(risk_usd or 0), 2), "lot": float(lot or 0),
           "state": "RISK_RESERVED", "active": True,
           "uncertain": False, "trade_id": None,
           "created_at": _now(), "updated_at": _now(),
           "transitions": [{"state": "RISK_RESERVED", "at": _now()}]}
    await db.risk_reservations.insert_one(dict(doc))
    return doc


async def transition(db, reservation_id: str, state: str,
                     idem_key: str | None = None, **extra) -> str:
    """audit P1 · guarded, fenced, idempotent reservation transition.

    Enforces: valid predecessor states, terminal-state protection (a
    RELEASED reservation can never re-activate), optional idempotency key,
    and matched-row verification. Returns applied|duplicate|invalid|missing.
    """
    prevs = ALLOWED_PREV.get(state)
    if prevs is None:
        raise ValueError(f"unknown reservation state: {state}")
    key = idem_key or f"{state}:{extra.get('release_reason') or ''}"
    q = {"reservation_id": reservation_id,
         "state": {"$in": list(prevs)},
         "transition_keys": {"$ne": key}}
    upd = {"state": state, "active": state in ACTIVE_STATES,
           "updated_at": _now(), **extra}
    res = await db.risk_reservations.update_one(
        q,
        {"$set": upd,
         "$addToSet": {"transition_keys": key},
         "$push": {"transitions": {"state": state, "at": _now(),
                                   **{k: v for k, v in extra.items()
                                      if k != "transitions"}}}})
    if res.modified_count == 1:
        return "applied"
    doc = await db.risk_reservations.find_one(
        {"reservation_id": reservation_id},
        {"state": 1, "transition_keys": 1})
    if doc is None:
        logger.warning("reservation transition: missing rid=%s", reservation_id)
        return "missing"
    if doc.get("state") == state or key in (doc.get("transition_keys") or []):
        return "duplicate"
    logger.warning("reservation transition REFUSED %s -> %s rid=%s",
                   doc.get("state"), state, reservation_id)
    return "invalid"


async def release_for_trade(db, trade_id: str, reason: str) -> None:
    """audit r3 P0 · unified release path: every release goes through the
    guarded transition() (state guard + idempotency + terminal RELEASED)."""
    async for r in db.risk_reservations.find(
            {"trade_id": trade_id, "state": {"$in": list(ACTIVE_STATES)}},
            {"reservation_id": 1}):
        await transition(db, r["reservation_id"], "RELEASED",
                         release_reason=reason)


async def unaccounted_count(db, account_id: str) -> int:
    """Active reservations INVISIBLE to the db.trades exposure count:
    uncertain submissions and reservations without a queued trade yet."""
    return await db.risk_reservations.count_documents(
        {"account_id": account_id, "state": {"$in": list(ACTIVE_STATES)},
         "$or": [{"uncertain": True}, {"trade_id": None}]})


async def sweep_stale(db, older_than_sec: int = STALE_TTL_SEC) -> dict:
    """Reconciliation: resolve reservations stuck active beyond the TTL."""
    cutoff = _now() - timedelta(seconds=older_than_sec)
    released = kept = 0
    async for r in db.risk_reservations.find(
            {"state": {"$in": list(ACTIVE_STATES)},
             "updated_at": {"$lt": cutoff}}):
        reason = None
        if r.get("uncertain"):
            kept += 1                          # N98-10 — exchange outcome UNKNOWN: only broker truth settles it
            continue
        if not r.get("trade_id"):
            reason = "expired"
        else:
            from bson import ObjectId
            try:
                tr = await db.trades.find_one(
                    {"_id": ObjectId(r["trade_id"])}, {"status": 1})
            except Exception:
                tr = None
            status = (tr or {}).get("status")
            if tr is None or status in ("closed", "failed", "cancelled"):
                reason = "reconciled"
            elif status == "open":
                reason = "broker_ack"          # position counted elsewhere
        if reason:
            await transition(db, r["reservation_id"], "RELEASED",
                             release_reason=reason)
            released += 1
        else:
            kept += 1                          # pending → still uncertain
    if released:
        logger.info("risk_reservations sweep: released=%d kept=%d",
                    released, kept)
    return {"released": released, "kept": kept}
