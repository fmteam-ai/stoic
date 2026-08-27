"""Facade the PAMM module uses to reach a broker — partner lookup + adapter."""
import uuid
from datetime import datetime, timezone

from services.broker_gateway.broker_adapter import BrokerAdapter, adapter_for

SANDBOX_PARTNER_ID = "prt_sandbox"


async def get_partner(db, partner_id: str) -> dict:
    p = await db.broker_partners.find_one({"partner_id": partner_id}, {"_id": 0})
    if not p:
        raise ValueError(f"unknown broker partner {partner_id}")
    return p


async def get_adapter(db, partner_id: str) -> BrokerAdapter:
    return adapter_for(db, await get_partner(db, partner_id))


async def ensure_sandbox_partner(db) -> dict:
    """Idempotent seed of the sandbox broker partner (webhook secret is
    encrypted at rest via secrets_vault)."""
    existing = await db.broker_partners.find_one(
        {"partner_id": SANDBOX_PARTNER_ID}, {"_id": 0})
    if existing:
        return existing
    import secrets_vault
    doc = {"partner_id": SANDBOX_PARTNER_ID, "name": "Sandbox Broker",
           "adapter": "sandbox", "status": "active",
           "webhook_secret_enc": secrets_vault.encrypt(
               uuid.uuid4().hex + uuid.uuid4().hex,
               associated_data=b"broker_webhook_secret"),
           "created_at": datetime.now(timezone.utc).isoformat()}
    await db.broker_partners.update_one(
        {"partner_id": SANDBOX_PARTNER_ID}, {"$setOnInsert": doc}, upsert=True)
    doc.pop("_id", None)
    return doc


def webhook_secret(partner: dict) -> str:
    import secrets_vault
    return secrets_vault.decrypt(partner["webhook_secret_enc"],
                                 associated_data=b"broker_webhook_secret")
