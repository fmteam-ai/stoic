import os
from datetime import datetime, timezone
from database import get_db
from auth import hash_password, verify_password


async def seed_admin():
    db = get_db()
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@trading.bot").lower()
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
    existing = await db.users.find_one({"email": admin_email})
    if not existing:
        await db.users.insert_one({
            "email": admin_email,
            "password_hash": hash_password(admin_password),
            "name": "Admin",
            "role": "admin",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    elif not verify_password(admin_password, existing["password_hash"]):
        await db.users.update_one(
            {"email": admin_email},
            {"$set": {"password_hash": hash_password(admin_password)}},
        )

    # Default bot config for admin (account_id=None marks the user-default profile)
    admin = await db.users.find_one({"email": admin_email})
    admin_id = str(admin["_id"])
    await db.bot_configs.update_one(
        {"user_id": admin_id, "$or": [{"account_id": None},
                                      {"account_id": {"$exists": False}}]},
        {"$setOnInsert": {
            "user_id": admin_id,
            "account_id": None,
            "risk_level": "medium",
            "symbols": ["XAUUSD", "BTCUSD"],
            "active": False,
            "max_concurrent_trades": 3,
            "auto_execute": True,
            "max_lot_size": 0.0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )

    # Backfill: any legacy bot_configs doc that pre-dates per-account scoping
    # has no `account_id`. Mark it as the user's default (None) so the new
    # filter (`account_id` null OR missing) keeps matching it.
    await db.bot_configs.update_many(
        {"account_id": {"$exists": False}},
        {"$set": {"account_id": None}},
    )


async def ensure_indexes():
    db = get_db()
    await db.users.create_index("email", unique=True)
    await db.accounts.create_index("bridge_token", unique=True)
    await db.accounts.create_index("user_id")
    await db.signals.create_index([("user_id", 1), ("created_at", -1)])
    await db.trades.create_index([("user_id", 1), ("opened_at", -1)])
    await db.trades.create_index([("account_id", 1), ("status", 1)])
    # bot_configs is now keyed by (user_id, account_id). account_id=None marks
    # the user's default profile; other docs are per-account overrides.
    # Drop the old unique(user_id) index if it exists, then create the composite.
    try:
        await db.bot_configs.drop_index("user_id_1")
    except Exception:
        pass  # index didn't exist on this deploy
    await db.bot_configs.create_index(
        [("user_id", 1), ("account_id", 1)], unique=True
    )
    await db.conditional_triggers.create_index([("user_id", 1), ("active", 1)])

    # --- Mongo Time-Series collections (TimescaleDB substitute) ---
    # Built-in since Mongo 5.0 — auto-bucketed, columnar storage, blazing fast
    # for time-windowed queries. Same RAM footprint as a regular insert.
    existing = await db.list_collection_names()
    if "price_ticks" not in existing:
        try:
            await db.create_collection(
                "price_ticks",
                timeseries={
                    "timeField": "ts",
                    "metaField": "symbol",
                    "granularity": "seconds",
                },
                expireAfterSeconds=60 * 60 * 24 * 7,  # auto-purge 7d
            )
        except Exception:
            pass  # already exists / older Mongo — fall back silently
    if "signal_history" not in existing:
        try:
            await db.create_collection(
                "signal_history",
                timeseries={
                    "timeField": "ts",
                    "metaField": "user_symbol",
                    "granularity": "minutes",
                },
                expireAfterSeconds=60 * 60 * 24 * 90,  # 90d
            )
        except Exception:
            pass
