"""audit r3 P0 · EA command sequence fencing.

Every EA-bound command (open / modify / close) carries:
  intent_id — immutable UUID identifying THIS command instance
  seq       — per-trade monotonically increasing counter ($inc, atomic)

Opens: the trade_id itself is the immutable intent (dispatch is fenced by
the _dispatched_at lock + lease epoch in poll-trades); modifications and
closes are stamped here. The modification-ack endpoint refuses acks whose
intent_id does not match the CURRENT pending command, so a delayed or
replayed ack can never apply an older stop over a newer one.
EA v1.49 spec: echo intent_id in every ack and keep the last 50 executed
intent_ids persisted; skip any command whose intent_id was already executed.
"""
import uuid
from datetime import datetime, timezone

from pymongo import ReturnDocument


async def stamp_pending_modification(db, filter_q: dict, mod: dict,
                                     extra_set: dict | None = None) -> dict | None:
    """Atomically stamp `mod` with intent_id + monotonic per-trade seq and
    set it as the trade's pending_modification (single pipeline update —
    the $inc'd counter and the command referencing it commit together).
    filter_q must include the no-command-in-flight guard the caller needs.
    Returns the stamped modification, or None if the filter did not match."""
    mod = dict(mod)
    mod["intent_id"] = uuid.uuid4().hex
    mod.setdefault("requested_at", datetime.now(timezone.utc).isoformat())
    static = {k: {"$literal": v} for k, v in mod.items()}
    set2 = {"pending_modification": {**static, "seq": "$command_seq"}}
    for k, v in (extra_set or {}).items():
        set2[k] = {"$literal": v}
    doc = await db.trades.find_one_and_update(
        filter_q,
        [{"$set": {"command_seq": {"$add": [{"$ifNull": ["$command_seq", 0]}, 1]}}},
         {"$set": set2}],
        return_document=ReturnDocument.AFTER,
        projection={"pending_modification": 1})
    return (doc or {}).get("pending_modification")


def is_stale_ack(trade: dict, ack_intent_id: str | None) -> bool:
    """True when the ack references an intent that is NOT the current
    pending command — a delayed/replayed ack that must be ignored.
    Acks without an intent_id (pre-v1.49 EAs) pass through unfenced."""
    if not ack_intent_id:
        return False
    current = (trade.get("pending_modification") or {}).get("intent_id")
    return current is not None and ack_intent_id != current
