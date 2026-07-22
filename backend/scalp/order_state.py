"""Phase A — explicit order state machine with FULL idempotency.

Formal lifecycle persisted on the trade document (`lifecycle_state` +
append-only `lifecycle` audit array + `lifecycle_keys` idempotency set):

    QUEUED → EA_CLAIMED → BROKER_ACCEPTED → FILLED_UNPROTECTED
           → (PROTECTION_REQUESTED) → PROTECTED → OPEN
           → CLOSE_REQUESTED → CLOSED → FINANCIALLY_RECONCILED
    (failure/uncertainty branches: REJECTED, UNCERTAIN)

Pre-order stages (CREATED → VALIDATED → RISK_RESERVED) live on the
scalp_decisions ledger + risk_reservations collection, which already record
them durably; the trade-document machine starts at QUEUED.

Every transition:
  · carries an idempotency key — re-applying the same key is a no-op
  · is guarded by the ALLOWED_PREV map — an out-of-order write is refused
  · appends an immutable audit entry with the key and metadata

Legacy trade docs (no lifecycle_state) may enter at any state so recovery
paths keep working across the deployment boundary.
"""
import logging
from datetime import datetime, timezone

from bson import ObjectId

logger = logging.getLogger(__name__)

QUEUED = "QUEUED"
EA_CLAIMED = "EA_CLAIMED"
BROKER_ACCEPTED = "BROKER_ACCEPTED"
FILLED_UNPROTECTED = "FILLED_UNPROTECTED"      # P0-1 · fill confirmed, SL not yet verified
PROTECTION_REQUESTED = "PROTECTION_REQUESTED"  # P0-1 · SL (re)arm sent to EA
PROTECTED = "PROTECTED"                        # P0-1 · broker confirms SL active
OPEN = "OPEN"
CLOSE_REQUESTED = "CLOSE_REQUESTED"
CLOSED = "CLOSED"
FINANCIALLY_RECONCILED = "FINANCIALLY_RECONCILED"
REJECTED = "REJECTED"
UNCERTAIN = "UNCERTAIN"

STATES = (QUEUED, EA_CLAIMED, BROKER_ACCEPTED, FILLED_UNPROTECTED,
          PROTECTION_REQUESTED, PROTECTED, OPEN, CLOSE_REQUESTED,
          CLOSED, FINANCIALLY_RECONCILED, REJECTED, UNCERTAIN)
TERMINAL = (FINANCIALLY_RECONCILED, REJECTED)

# state -> tuple of states it may be entered FROM (empty = entry state)
ALLOWED_PREV = {
    QUEUED: (),
    EA_CLAIMED: (QUEUED,),
    BROKER_ACCEPTED: (QUEUED, EA_CLAIMED, UNCERTAIN),
    # P0-1 · a filled position is NOT safe until MT5 confirms its stop is live
    FILLED_UNPROTECTED: (QUEUED, EA_CLAIMED, BROKER_ACCEPTED, UNCERTAIN),
    PROTECTION_REQUESTED: (FILLED_UNPROTECTED,),
    PROTECTED: (BROKER_ACCEPTED, FILLED_UNPROTECTED, PROTECTION_REQUESTED),
    OPEN: (PROTECTED,),  # audit P0 · OPEN strictly requires confirmed protection
    CLOSE_REQUESTED: (QUEUED, EA_CLAIMED, BROKER_ACCEPTED, FILLED_UNPROTECTED,
                      PROTECTION_REQUESTED, PROTECTED, OPEN, UNCERTAIN),
    CLOSED: (QUEUED, EA_CLAIMED, BROKER_ACCEPTED, FILLED_UNPROTECTED,
             PROTECTION_REQUESTED, PROTECTED, OPEN, CLOSE_REQUESTED,
             UNCERTAIN),
    # the authoritative broker deal may arrive before every operational ack
    FINANCIALLY_RECONCILED: (QUEUED, EA_CLAIMED, BROKER_ACCEPTED,
                             FILLED_UNPROTECTED, PROTECTION_REQUESTED,
                             PROTECTED, OPEN, CLOSE_REQUESTED, CLOSED,
                             UNCERTAIN),
    REJECTED: (QUEUED, EA_CLAIMED, UNCERTAIN),
    UNCERTAIN: (QUEUED, EA_CLAIMED, BROKER_ACCEPTED),
}


def can_transition(current: str | None, new: str) -> bool:
    if new not in ALLOWED_PREV:
        return False
    if current is None:                 # legacy doc — recovery may enter
        return True
    return current in ALLOWED_PREV[new]


def _oid(trade_id):
    try:
        return ObjectId(str(trade_id))
    except Exception:
        return trade_id


async def apply(db, trade_id: str, to_state: str, idem_key: str,
                meta: dict | None = None) -> str:
    """Idempotent, guarded transition. Returns one of:
    'applied' | 'duplicate' (idem key already used) |
    'invalid' (transition not allowed from current state) | 'missing' |
    'quarantined' (audit r3 · unverified stateless record).

    Stateless docs (no lifecycle_state) get permissive entry ONLY when
    verified legacy: lifecycle_version already stamped, legacy_trade marker,
    or created before the state machine rollout (LEGACY_EPOCH). New
    malformed records are quarantined instead of silently adopted."""
    if to_state not in ALLOWED_PREV:
        raise ValueError(f"unknown order state {to_state}")
    now = datetime.now(timezone.utc)
    prevs = list(ALLOWED_PREV[to_state])
    entry_state = not prevs   # QUEUED — the only legitimate NEW-trade entry
    legacy_ok = {"$or": [{"lifecycle_version": {"$gte": 1}},
                         {"legacy_trade": True},
                         {"created_at": {"$lt": LEGACY_EPOCH}},
                         {"opened_at": {"$lt": LEGACY_EPOCH}}]}
    stateless = {"$or": [{"lifecycle_state": {"$exists": False}},
                         {"lifecycle_state": None}]}
    state_guard: dict = {"$or": [
        # stateless entry: any new doc may start at QUEUED; deeper states
        # only for VERIFIED legacy records
        ({"$and": [stateless]} if entry_state
         else {"$and": [stateless, legacy_ok]}),
    ]}
    if prevs:
        state_guard["$or"].append({"lifecycle_state": {"$in": prevs}})
    res = await db.trades.update_one(
        {"_id": _oid(trade_id), "lifecycle_keys": {"$ne": idem_key},
         **state_guard},
        {"$set": {"lifecycle_state": to_state, "lifecycle_at": now,
                  "lifecycle_version": 1},
         "$push": {"lifecycle": {"state": to_state, "at": now,
                                 "key": idem_key, **(meta or {})},
                   # audit P1 · SAME-DOCUMENT embedded outbox: the lifecycle
                   # transition and its event become durable in ONE atomic
                   # write (single-doc atomicity — no replica-set txn
                   # needed); the outbox relay drains + publishes them.
                   "outbox_events": {
                       "event_id": f"lc:{trade_id}:{idem_key}",
                       "event_type": f"Lifecycle{to_state.title().replace('_', '')}",
                       "state": to_state, "at": now,
                       "meta": meta or {}, "published": False}},
         "$addToSet": {"lifecycle_keys": idem_key}})
    if res.modified_count == 1:
        return "applied"
    doc = await db.trades.find_one(
        {"_id": _oid(trade_id)},
        {"lifecycle_state": 1, "lifecycle_keys": 1, "lifecycle_version": 1,
         "legacy_trade": 1, "created_at": 1, "opened_at": 1})
    if doc is None:
        return "missing"
    if idem_key in (doc.get("lifecycle_keys") or []):
        return "duplicate"
    if doc.get("lifecycle_state") is None and not entry_state:
        # stateless AND failed the legacy verification → quarantine
        await db.trades.update_one(
            {"_id": _oid(trade_id), "lifecycle_state": None},
            {"$set": {"lifecycle_quarantined": True,
                      "lifecycle_quarantine_reason":
                          f"unverified stateless record; refused {to_state}",
                      "lifecycle_quarantined_at": now}})
        logger.error("order_state: QUARANTINED stateless trade=%s "
                     "(refused %s — not verified legacy)", trade_id, to_state)
        return "quarantined"
    logger.warning("order_state: invalid transition %s -> %s trade=%s",
                   doc.get("lifecycle_state"), to_state, trade_id)
    return "invalid"
