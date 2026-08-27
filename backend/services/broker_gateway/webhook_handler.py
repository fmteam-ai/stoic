"""Inbound broker webhook processing: verify → dedupe → event log."""
import json
from datetime import datetime, timezone

from services.broker_gateway.auth import verify_webhook
from services.broker_gateway.pamm_api import get_partner, webhook_secret

ACCEPTED_TYPES = {
    "DepositReceived", "WithdrawalRequested", "AllocationUpdated",
    "TradeOpened", "TradeClosed", "NAVUpdated", "FeeCalculated",
    "HeartbeatLost", "BrokerDisconnected",
}


async def handle_webhook(db, partner_id: str, headers: dict,
                         body: bytes) -> dict:
    partner = await get_partner(db, partner_id)
    ok, reason = verify_webhook(
        webhook_secret(partner),
        headers.get("x-broker-timestamp", ""),
        headers.get("x-broker-signature", ""), body)
    if not ok:
        raise PermissionError(reason)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise ValueError("invalid JSON body")
    etype = str(payload.get("type") or "")
    event_key = str(payload.get("event_id") or "")
    if etype not in ACCEPTED_TYPES:
        raise ValueError(f"unsupported event type: {etype}")
    if not event_key:
        raise ValueError("event_id required (idempotency key)")
    from modules.pamm.events import emit_event
    emitted = await emit_event(
        db, etype, dict(payload.get("data") or {}),
        source=f"webhook:{partner_id}", event_key=f"{partner_id}:{event_key}")
    if emitted is None:
        return {"accepted": True, "duplicate": True}
    await db.pamm_notifications.insert_one({
        "type": etype, "partner_id": partner_id,
        "at": datetime.now(timezone.utc).isoformat(), "seen": False,
        "summary": f"{etype} from {partner['name']}"})
    return {"accepted": True, "duplicate": False}
