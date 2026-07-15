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
    # Round 8 item 1 — scalp reconciliation / ownership integrity constraints
    await db.scalp_owners.create_index("account_id", unique=True)
    await db.broker_deals.create_index([("account_id", 1), ("deal_id", 1)],
                                       unique=True)
    await db.broker_deals.create_index([("financial_reconciliation_status", 1),
                                        ("received_at", 1)])
    await db.scalp_risk_state.create_index([("account_id", 1), ("symbol", 1)],
                                           unique=True)
    await db.scalp_financial_events.create_index([("account_id", 1), ("at", -1)])
    await db.scalp_financial_events.create_index(
        [("account_id", 1), ("deal_id", 1), ("event_type", 1)], unique=True)
    await db.broker_time_offsets.create_index([("account_id", 1),
                                               ("effective_from", -1)])

    # broker_deals — idempotency log of every MT5 deal reported via
    # /bridge/external-deal. Unique on (deal_id, account_id) so a single
    # broker deal can only be applied once per account, even if the EA retries.
    await db.broker_deals.create_index(
        [("deal_id", 1), ("account_id", 1)], unique=True
    )
    await db.broker_deals.create_index([("user_id", 1), ("received_at", -1)])

    # --- iter-39: 90-day TTL on agent_activity ---
    # agent_activity grows ~1 row/tick/user. With 1000+ users this is ~525k
    # rows/year. Cap retention at 90d so the DB stays small. expireAfterSeconds
    # is set on the `started_at` index — Mongo's TTL monitor sweeps every 60s.
    # NB: Only applied to collections whose timestamp field is a real BSON
    # Date (not ISO string). Signals/safety_blocks store ISO strings so TTL
    # would silently no-op on them — they'd need a model migration first.
    try:
        await db.agent_activity.create_index(
            "started_at", expireAfterSeconds=90 * 24 * 3600,
            name="agent_activity_ttl_90d",
        )
    except Exception:
        pass  # index may already exist with different settings

    # --- High-volume tick / signal history collections -------------------
    # NOTE: We deliberately do NOT use MongoDB time-series collections here.
    # On managed Atlas deployments, the dump-and-restore migration path the
    # platform uses cannot write to the internal `system.buckets.*` namespaces
    # (the restore user lacks the privileged role), which broke our first
    # production deploy (see iter-89 root cause). Regular collections + a
    # TTL index on `ts` give us identical auto-purge behaviour with a
    # universally-portable schema.
    existing = await db.list_collection_names()
    if "price_ticks" not in existing:
        try:
            await db.create_collection("price_ticks")
        except Exception:
            pass
    try:
        # auto-purge 7 days — matches the old timeseries expireAfterSeconds
        await db.price_ticks.create_index("ts", expireAfterSeconds=60 * 60 * 24 * 7)
        await db.price_ticks.create_index([("symbol", 1), ("ts", -1)])
    except Exception:
        pass
    if "signal_history" not in existing:
        try:
            await db.create_collection("signal_history")
        except Exception:
            pass
    try:
        # auto-purge 90 days
        await db.signal_history.create_index("ts", expireAfterSeconds=60 * 60 * 24 * 90)
        await db.signal_history.create_index([("user_symbol", 1), ("ts", -1)])
    except Exception:
        pass

    # --- iter-91 · Heal stale panic / circuit-breaker trip flags ---------
    # Background: the original POST /api/bot/start endpoint flipped
    # `active=True` when a user re-enabled a panic-tripped bot but failed
    # to clear `tripped_at` / `tripped_reason`. The bot ran fine, but the
    # dashboard kept showing "PANIC LOCK — all trading halted by user/admin"
    # forever. We fixed the endpoint, and now this startup hook self-heals
    # any pre-existing stale flags so users don't have to toggle Stop/Start
    # on every account post-deploy. Idempotent — runs every boot; no-op once
    # the DB is clean.
    try:
        r = await db.bot_configs.update_many(
            {"active": True, "tripped_reason": {"$exists": True}},
            {"$unset": {"tripped_at": "", "tripped_reason": ""}},
        )
        if r.modified_count:
            print(f"seed: auto-healed {r.modified_count} bot_config(s) with stale panic flags")
    except Exception:
        pass
