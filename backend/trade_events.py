"""Immutable trade lifecycle event stream — event-sourcing foundation
(review item 4).

Append-only `trade_events` collection written ALONGSIDE the existing
collections (which remain the authoritative projections for now). Enables
replay, audit and per-trade lifecycle reconstruction from a single stream:

    DecisionCreated → RiskApproved → OrderIntentCreated → BrokerSubmitted
    → BrokerAccepted/BrokerRejected → PositionOpened → ProtectionPlaced
    → PositionClosed → FinancialApplied
"""
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SCHEMA_V = 1

EVENT_TYPES = (
    "DecisionCreated", "RiskApproved", "OrderIntentCreated",
    "BrokerSubmitted", "BrokerAccepted", "BrokerRejected",
    "PositionOpened", "ProtectionPlaced", "PositionClosed",
    "FinancialApplied",
    "StopModifyConfirmed", "StopModifyRejected",   # round 18 item 2
    # Phase 6 · adaptive-exit + modification lifecycle (execution trace)
    "TargetsRescaled", "StopTightened", "PartialCloseRequested",
    "ModificationConfirmed",
)

_indexed = False


def build(event_type: str, *, user_id: str | None = None,
          decision_id: str | None = None, trade_id: str | None = None,
          account_id: str | None = None, symbol: str | None = None,
          source: str = "scalp_engine", payload: dict | None = None) -> dict:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event_type {event_type}")
    now = datetime.now(timezone.utc)
    return {
        "event_id": uuid.uuid4().hex,
        "event_type": event_type,
        "schema_v": SCHEMA_V,
        "occurred_at": now.isoformat(),
        "ts_ms": int(now.timestamp() * 1000),
        "user_id": user_id,
        "decision_id": decision_id,
        "trade_id": trade_id,
        "account_id": account_id,
        "symbol": symbol,
        "source": source,
        "payload": payload or {},
    }


async def append(db, event: dict) -> None:
    # Phase 8 — tamper-evident hash chain per user: each event carries the
    # previous event's hash; any later mutation breaks the chain.
    try:
        import hashlib
        import json as _json
        prev = await db.trade_events.find_one(
            {"user_id": event.get("user_id")},
            sort=[("ts_ms", -1), ("_id", -1)])
        prev_hash = (prev or {}).get("hash") or "genesis"
        body = {k: event.get(k) for k in
                ("event_type", "user_id", "trade_id", "account_id",
                 "symbol", "source", "ts_ms", "occurred_at", "payload")}
        event["prev_hash"] = prev_hash
        event["hash"] = hashlib.sha256(
            (prev_hash + _json.dumps(body, sort_keys=True, default=str)
             ).encode()).hexdigest()
    except Exception:  # noqa: BLE001 — chain is best-effort, never blocks
        pass
    """Insert-only. Events are never updated or deleted."""
    global _indexed
    if not _indexed:
        _indexed = True
        try:
            await db.trade_events.create_index([("decision_id", 1), ("ts_ms", 1)])
            await db.trade_events.create_index([("trade_id", 1), ("ts_ms", 1)])
            await db.trade_events.create_index([("event_type", 1), ("ts_ms", -1)])
        except Exception:
            logger.exception("trade_events index creation failed")
    await db.trade_events.insert_one(dict(event))
