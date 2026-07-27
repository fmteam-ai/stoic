import os
import logging
from datetime import datetime, timezone
from database import get_db
from auth import hash_password, verify_password


async def dependency_health_check() -> dict:
    """Round 15 item 10 — startup dependency + index health check. Verifies
    the driver stack (bson/pymongo/motor), a live DB ping, and that the
    safety-critical unique indexes actually exist. Logs CRITICAL on any
    failure so a bad deploy is visible immediately."""
    import logging
    log = logging.getLogger("startup-health")
    result = {"deps_ok": True, "db_ok": False, "indexes_ok": False,
              "missing_indexes": []}
    try:
        import bson  # noqa: F401
        import pymongo  # noqa: F401
        import motor  # noqa: F401
    except ImportError as e:
        result["deps_ok"] = False
        log.critical("driver dependency missing: %s", e)
        return result
    db = get_db()
    try:
        await db.command("ping")
        result["db_ok"] = True
    except Exception as e:  # noqa: BLE001
        log.critical("MongoDB ping failed: %s", e)
        return result
    critical = {"users": "email", "accounts": "bridge_token",
                "trades": "account_id", "scalp_owners": "account_id",
                "scalp_decisions": "decision_id",
                "scalp_submission_slots": "broker_key"}
    missing = []
    for coll, field in critical.items():
        try:
            info = await db[coll].index_information()
            if not any(field in str(k) for k in info):
                missing.append(f"{coll}.{field}")
        except Exception as e:  # noqa: BLE001
            missing.append(f"{coll} (unreadable: {e})")
    result["missing_indexes"] = missing
    result["indexes_ok"] = not missing
    if missing:
        log.critical("critical indexes missing/unverifiable: %s", missing)
    else:
        log.info("dependency + index health check passed")
    return result


async def seed_admin():
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@stoicaibot.com").lower()
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
    is_prod = os.environ.get("APP_ENV", "").lower() in ("production", "prod")
    # SEC-001 — never ship a known-weak admin in production, and never force
    # an admin password back to the env value once the account exists (that
    # made password changes impossible across restarts).
    if is_prod:
        if not os.environ.get("ADMIN_PASSWORD") or admin_password == "admin123":
            raise RuntimeError(
                "APP_ENV=production requires a strong ADMIN_PASSWORD "
                "(the default 'admin123' is refused).")
        if len(admin_password) < 12:
            raise RuntimeError(
                "ADMIN_PASSWORD must be at least 12 characters in production.")
    db = get_db()
    existing = await db.users.find_one({"email": admin_email})
    if not existing:
        await db.users.insert_one({
            "email": admin_email,
            "password_hash": hash_password(admin_password),
            "name": "Admin",
            "role": "admin",
            # one-time bootstrap credential (installer secret file) — the
            # operator must replace it at first login
            # Force a first-login password rotation for the freshly-seeded
            # admin in production (or when a bootstrap password file is used),
            # so the initial ADMIN_PASSWORD is never a standing credential.
            "must_change_password": bool(
                is_prod or os.environ.get("ADMIN_PASSWORD_FILE")),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        log = logging.getLogger("seed")
        log.info("seeded admin account %s", admin_email)
    # Do NOT overwrite an existing admin hash — the admin can change their
    # password and it must survive restarts.

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


async def _migrate_iso_strings_to_bson_dates(db):
    """One-time (idempotent) migration: ops/lifecycle timestamps written as
    ISO strings by earlier builds become BSON UTC datetimes so Mongo can do
    real date comparisons and TTL expiry."""
    from datetime import datetime as _dt

    def _conv(v):
        try:
            ts = _dt.fromisoformat(str(v))
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    plans = (("worker_leases", ("expires_at", "renewed_at")),
             ("ops_alerts", ("created_at", "last_seen_at", "acked_at")),
             ("validation_evidence", ("recorded_at",)),
             ("trade_journal_cards", ("created_at", "edited_at")))
    for coll, fields in plans:
        async for doc in db[coll].find(
                {"$or": [{f: {"$type": "string"}} for f in fields]}):
            sets = {f: _conv(doc[f]) for f in fields
                    if isinstance(doc.get(f), str) and _conv(doc[f])}
            if sets:
                await db[coll].update_one({"_id": doc["_id"]}, {"$set": sets})
    stage = await db.platform_state.find_one({"_id": "deployment_stage"})
    if stage:
        sets = {}
        if isinstance(stage.get("entered_at"), str) and _conv(stage["entered_at"]):
            sets["entered_at"] = _conv(stage["entered_at"])
        hist = stage.get("history") or []
        changed = False
        for h in hist:
            if isinstance(h.get("at"), str) and _conv(h["at"]):
                h["at"] = _conv(h["at"])
                changed = True
        if changed:
            sets["history"] = hist
        if sets:
            await db.platform_state.update_one(
                {"_id": "deployment_stage"}, {"$set": sets})


async def ensure_indexes():
    db = get_db()
    await db.users.create_index("email", unique=True)
    await db.accounts.create_index("bridge_token", unique=True)
    await db.accounts.create_index("user_id")
    # ops collections — BSON-date native (TTL prunes acked alerts after 30d)
    await db.ops_alerts.create_index([("dedup_key", 1), ("acked_at", 1)])
    await db.ops_alerts.create_index("acked_at", expireAfterSeconds=2592000)
    await db.validation_evidence.create_index([("scenario", 1), ("recorded_at", 1)])
    await _migrate_iso_strings_to_bson_dates(db)
    # iter-163 — audit_log.at must be ISO strings everywhere: mixed BSON
    # dates sorted ABOVE strings in Mongo type order, hiding newer entries
    # from the /auth/audit view. Idempotent (0 matches after first run).
    await db.audit_log.update_many(
        {"at": {"$type": "date"}},
        [{"$set": {"at": {"$dateToString": {
            "date": "$at", "format": "%Y-%m-%dT%H:%M:%S.%L+00:00"}}}}])
    await db.audit_log.create_index([("user_id", 1), ("at", -1)])
    await db.signals.create_index([("user_id", 1), ("created_at", -1)])
    await db.trades.create_index([("user_id", 1), ("opened_at", -1)])
    await db.trades.create_index([("account_id", 1), ("status", 1)])
    # Round 15 item 10 — decision updates key on decision_id everywhere;
    # without this index every update is a collection scan (caught by
    # dependency_health_check on first deploy). Partial: legacy docs
    # without a decision_id are exempt from the uniqueness constraint.
    await db.scalp_decisions.create_index(
        "decision_id", unique=True,
        partialFilterExpression={"decision_id": {"$type": "string"}})
    await db.scalp_decisions.create_index([("account_id", 1), ("symbol", 1),
                                           ("ts_ms", -1)])
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
    # Round 8 item 1 / Round 10 item 5 — scalp reconciliation / ownership
    # integrity constraints. These unique indexes ARE the distributed-safety
    # guarantees (single lease owner, exactly-once deal application, one
    # risk snapshot per account+symbol, one ledger event per deal+type).
    # Failure to create ANY of them is FATAL for the scalp service: the
    # whole subsystem fails closed until indexes are healthy.
    try:
        await db.scalp_owners.create_index("account_id", unique=True)
        # iter-122 billing correctness — exactly-once payment ledger,
        # idempotent affiliate commissions, durable commission outbox,
        # one subscription doc per user, TTL cleanup of short-lived
        # VPS credentials (BSON-date expiries).
        await db.payment_transactions.create_index("session_id", unique=True)
        await db.affiliate_commissions.create_index(
            [("session_id", 1), ("tier", 1)], unique=True, sparse=True)
        await db.affiliate_outbox.create_index("session_id", unique=True)
        await db.subscriptions.create_index("user_id", unique=True)
        await db.ea_pairing_codes.create_index(
            "expires_at", expireAfterSeconds=3600)
        await db.vps_bootstrap_tokens.create_index(
            "expires_at", expireAfterSeconds=3600)
        # Round 17 item 7 — one document per capacity unit is a SAFETY
        # guarantee: duplicates would double broker capacity.
        await db.scalp_submission_slots.create_index(
            [("broker_key", 1), ("slot_id", 1)], unique=True)
        await db.trades.create_index("submission_slot.token", sparse=True)
        # Auth hardening: revocable refresh sessions + shared rate limits
        await db.auth_sessions.create_index("jti", unique=True)
        await db.auth_sessions.create_index([("user_id", 1), ("revoked", 1)])
        await db.auth_sessions.create_index("expires_at",
                                            expireAfterSeconds=0)
        await db.rate_limits.create_index("expires_at", expireAfterSeconds=0)
        await db.broker_deals.create_index([("account_id", 1), ("deal_id", 1)],
                                           unique=True)
        await db.broker_deals.create_index([("financial_reconciliation_status", 1),
                                            ("received_at", 1)])
        await db.scalp_risk_state.create_index([("account_id", 1), ("symbol", 1)],
                                               unique=True)
        await db.scalp_financial_events.create_index([("account_id", 1), ("at", -1)])
        await db.scalp_financial_events.create_index(
            [("account_id", 1), ("deal_id", 1), ("event_type", 1)], unique=True)
        # Round 18 review item 8 — reservation uniqueness constraints are a
        # safety guarantee (one active reservation per decision / trade).
        from scalp.risk_reservations import ensure_reservation_indexes
        await ensure_reservation_indexes(db)
        # Phase A — transactional-outbox durability for critical events
        from scalp.outbox import ensure_outbox_indexes
        await ensure_outbox_indexes(db)
        from scalp.engine import set_service_block
        set_service_block(None)
    except Exception as e:  # noqa: BLE001
        from scalp.engine import set_service_block
        set_service_block(f"critical index creation failed: {e}")
        logging.getLogger("trading-bot").critical(
            "SCALP SERVICE BLOCKED — unique index creation failed: %s", e)
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
