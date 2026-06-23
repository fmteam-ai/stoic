"""
User Presets — per-user saved bot configurations ("clone Sniper, tweak, save as 'My Sniper'").

Companion to /app/backend/strategy_presets.py (which holds built-in presets).
Built-ins are immutable templates; user presets are user-owned, mutable, and
scoped to a single user_id.

Storage: MongoDB `user_presets` collection.
Schema:
  {
    _id, user_id, name, description,
    config: { ...the same preset.config dict ...},
    created_at, updated_at,
  }
"""
from datetime import datetime, timezone
from bson import ObjectId
from database import get_db

# Which fields a saved preset is allowed to capture (whitelist).
# Mirrors strategy_presets.py — same behaviour knobs only. Never lets users
# accidentally serialise risk_level, symbols, drawdown limits, or per-symbol caps.
PRESET_FIELD_WHITELIST = {
    "aggressive_mode", "min_confidence_override",
    "trade_of_day_cap", "max_concurrent_trades",
    "trailing_enabled", "trailing_start_r", "trailing_distance_r",
    "partial_close_enabled", "partial_close_trigger_r", "partial_close_fraction",
    "breakeven_enabled", "breakeven_trigger_r",
    "sl_cooldown_enabled", "sl_cooldown_minutes",
    "pre_news_protect_enabled", "pre_news_protect_minutes",
    "anti_tilt_enabled", "anti_tilt_consecutive_losses", "anti_tilt_freeze_hours",
    "asia_session_skip_xau",
}

MAX_PRESETS_PER_USER = 10
MAX_NAME_LEN = 40
MAX_DESC_LEN = 200


def _now():
    return datetime.now(timezone.utc).isoformat()


def _serialize(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "user_id": doc["user_id"],
        "name": doc.get("name", ""),
        "description": doc.get("description", ""),
        "config": doc.get("config", {}),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "is_custom": True,
    }


def _sanitize_config(cfg: dict) -> dict:
    """Keep only whitelisted behaviour-knob fields from a full bot_config."""
    return {k: v for k, v in (cfg or {}).items() if k in PRESET_FIELD_WHITELIST}


async def list_user_presets(user_id: str) -> list[dict]:
    db = get_db()
    cursor = db.user_presets.find({"user_id": user_id}).sort("created_at", -1).limit(50)
    docs = await cursor.to_list(length=50)
    return [_serialize(d) for d in docs]


async def create_user_preset(*, user_id: str, name: str, description: str,
                             config: dict) -> dict:
    db = get_db()
    name = (name or "").strip()[:MAX_NAME_LEN]
    description = (description or "").strip()[:MAX_DESC_LEN]
    if not name:
        raise ValueError("Preset name required")

    count = await db.user_presets.count_documents({"user_id": user_id})
    if count >= MAX_PRESETS_PER_USER:
        raise ValueError(f"Limit reached — max {MAX_PRESETS_PER_USER} custom presets per user. Delete one first.")

    # Block name collisions for the same user
    existing = await db.user_presets.find_one({"user_id": user_id, "name": name})
    if existing:
        raise ValueError(f"You already have a preset named '{name}' — pick a different name.")

    doc = {
        "user_id": user_id,
        "name": name,
        "description": description,
        "config": _sanitize_config(config),
        "created_at": _now(),
        "updated_at": _now(),
    }
    r = await db.user_presets.insert_one(doc)
    doc["_id"] = r.inserted_id
    return _serialize(doc)


async def delete_user_preset(*, user_id: str, preset_id: str) -> bool:
    db = get_db()
    try:
        oid = ObjectId(preset_id)
    except Exception:
        return False
    r = await db.user_presets.delete_one({"_id": oid, "user_id": user_id})
    return r.deleted_count > 0


async def get_user_preset(*, user_id: str, preset_id: str) -> dict | None:
    db = get_db()
    try:
        oid = ObjectId(preset_id)
    except Exception:
        return None
    doc = await db.user_presets.find_one({"_id": oid, "user_id": user_id})
    return _serialize(doc) if doc else None
