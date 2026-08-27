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


async def register_partner(db, payload: dict, actor: str) -> dict:
    """Register a REAL broker partner (rest / mt5_manager). All secrets
    are encrypted at rest via the vault and never returned."""
    import secrets_vault
    adapter = str(payload.get("adapter") or "")
    if adapter not in ("rest", "mt5_manager"):
        raise ValueError("adapter must be 'rest' or 'mt5_manager'")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("name required")
    doc = {"partner_id": f"prt_{uuid.uuid4().hex[:8]}", "name": name[:120],
           "adapter": adapter, "status": "active", "created_by": actor,
           "webhook_secret_enc": secrets_vault.encrypt(
               uuid.uuid4().hex + uuid.uuid4().hex,
               associated_data=b"broker_webhook_secret"),
           "created_at": datetime.now(timezone.utc).isoformat()}
    if adapter == "rest":
        cfg = dict(payload.get("rest_config") or {})
        if not cfg.get("base_url"):
            raise ValueError("rest_config.base_url required")
        api_key = cfg.pop("api_key", None)
        if not api_key:
            raise ValueError("rest_config.api_key required")
        cfg["api_key_enc"] = secrets_vault.encrypt(
            str(api_key), associated_data=b"broker_api_key")
        doc["rest_config"] = cfg
    else:
        cfg = dict(payload.get("mt5_config") or {})
        for req in ("gateway_url", "manager_login", "server"):
            if not cfg.get(req):
                raise ValueError(f"mt5_config.{req} required")
        pw = cfg.pop("manager_password", None)
        if not pw:
            raise ValueError("mt5_config.manager_password required")
        cfg["manager_password_enc"] = secrets_vault.encrypt(
            str(pw), associated_data=b"broker_manager_password")
        doc["mt5_config"] = cfg
    await db.broker_partners.insert_one(dict(doc))
    return redact_partner({k: v for k, v in doc.items() if k != "_id"})


def redact_partner(p: dict) -> dict:
    out = dict(p)
    out.pop("webhook_secret_enc", None)
    for cfg_key, secret in (("rest_config", "api_key_enc"),
                            ("mt5_config", "manager_password_enc")):
        cfg = out.get(cfg_key)
        if isinstance(cfg, dict):
            cfg = dict(cfg)
            if cfg.pop(secret, None):
                cfg["credentials_set"] = True
            out[cfg_key] = cfg
    return out
