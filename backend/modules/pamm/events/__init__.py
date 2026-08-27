"""Mongo-backed PAMM event stream (Phases 14/15) — append-only, replayable,
idempotent via event_key. Upgradeable to Redis Streams later without
changing emitters."""
import uuid
from datetime import datetime, timezone

EVENT_TYPES = {
    "DepositReceived", "WithdrawalRequested", "AllocationUpdated",
    "TradeOpened", "TradeClosed", "NAVUpdated", "FeeCalculated",
    "HeartbeatLost", "StrategyPaused", "StrategyResumed", "EmergencyStop",
    "BrokerDisconnected", "VersionUpdated", "ProgramCreated",
    "InvestorCreated", "ReconciliationDrift",
    "RiskLimitBreached", "PositionsFlattened", "BrokerHealthDegraded",
    "JoinRequested", "JoinApproved", "JoinRejected",
    "OpStateChanged", "ChangeRequested", "ChangeApproved", "ChangeRejected",
    "FlattenFailed", "FlattenResolved",
    "BrokerIncidentOpened", "BrokerIncidentResolved", "ExternalEscalation",
}


async def emit_event(db, etype: str, data: dict, source: str = "stoic",
                     event_key: str | None = None) -> dict | None:
    """Returns the event, or None if event_key was already consumed."""
    if etype not in EVENT_TYPES:
        raise ValueError(f"unknown PAMM event type: {etype}")
    doc = {"event_id": f"evt_{uuid.uuid4().hex[:12]}", "type": etype,
           "data": data, "source": source,
           "at": datetime.now(timezone.utc).isoformat()}
    if event_key:
        doc["event_key"] = event_key
    try:
        await db.pamm_events.insert_one(dict(doc))
    except Exception:  # duplicate event_key → idempotent no-op
        if event_key and await db.pamm_events.find_one(
                {"event_key": event_key}, {"_id": 1}):
            return None
        raise
    doc.pop("_id", None)
    return doc


async def recent_events(db, program_id: str | None = None,
                        limit: int = 50) -> list:
    q = {"data.program_id": program_id} if program_id else {}
    return [e async for e in
            db.pamm_events.find(q, {"_id": 0}).sort("at", -1).limit(limit)]
