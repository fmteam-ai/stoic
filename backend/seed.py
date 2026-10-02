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


# sha256 of well-known weak defaults — the literals never appear in source
_KNOWN_DEFAULT_PW_HASHES = {
    "240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9",
    "8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92",
    "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8",
}


def _is_known_default_password(pw: str) -> bool:
    import hashlib
    return hashlib.sha256((pw or "").encode()).hexdigest() in _KNOWN_DEFAULT_PW_HASHES


async def seed_admin():
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@stoicaibot.com").lower()
    admin_password = os.environ.get("ADMIN_PASSWORD") or ""
    if not admin_password:
        logging.getLogger("seed").warning(
            "ADMIN_PASSWORD not set — admin account NOT seeded (fail closed)")
        return
    from app_env import is_production
    is_prod = is_production()
    # SEC-001 — never ship a known-weak admin in production, and never force
    # an admin password back to the env value once the account exists (that
    # made password changes impossible across restarts).
    if is_prod:
        if _is_known_default_password(admin_password):
            raise RuntimeError(
                "APP_ENV=production requires a strong ADMIN_PASSWORD "
                "(well-known default passwords are refused).")
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
    elif str(os.environ.get("ADMIN_PASSWORD_FORCE_RESET", "")
             ).lower() in ("1", "true", "yes"):
        # EXPLICIT operator recovery path (locked-out admin). One-shot PER
        # SECRET VALUE: a fingerprint on the user doc ensures each env
        # password is applied at most once even if the flag stays set, so
        # an in-app password change is never silently reverted afterwards.
        import hashlib
        fp = hashlib.sha256(admin_password.encode()).hexdigest()
        if existing.get("admin_env_pw_fingerprint") != fp:
            await db.users.update_one(
                {"_id": existing["_id"]},
                {"$set": {"password_hash": hash_password(admin_password),
                          "must_change_password": True,
                          "admin_env_pw_fingerprint": fp},
                 "$unset": {"password_reset_token": "",
                            "password_reset_token_sha256": "",
                            "password_reset_expires_at": ""}})
            await db.login_attempts.delete_many(
                {"identifier": {"$regex": admin_email.replace(".", r"\.")}})
            logging.getLogger("seed").warning(
                "ADMIN_PASSWORD_FORCE_RESET applied for %s — password "
                "re-synced from env, first-login rotation enforced. "
                "Remove the flag after recovery.", admin_email)
    # Otherwise: NEVER overwrite an existing admin hash — the admin can
    # change their password and it must survive restarts.

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


async def invalidate_legacy_plaintext_tokens():
    """r25 P2-03 — pre-v96 activation / password-reset tokens were stored in
    plaintext; v96 matches by SHA-256 only, so those links can never verify.
    Invalidate them EXPLICITLY (one-shot, idempotent) and remember that the
    user held a legacy link so the API can answer "link superseded — request a
    new one" instead of a generic invalid-token error."""
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    r1 = await db.users.update_many(
        {"activation_token": {"$exists": True}, "activation_token_sha256": {"$exists": False}},
        {"$unset": {"activation_token": "", "activation_expires_at": ""},
         "$set": {"legacy_activation_invalidated_at": now}})
    r2 = await db.users.update_many(
        {"password_reset_token": {"$exists": True}, "password_reset_token_sha256": {"$exists": False}},
        {"$unset": {"password_reset_token": "", "password_reset_expires_at": ""},
         "$set": {"legacy_reset_invalidated_at": now}})
    if r1.modified_count or r2.modified_count:
        logging.getLogger("seed").warning(
            "legacy plaintext tokens invalidated — activation=%d reset=%d (users must request new links)",
            r1.modified_count, r2.modified_count)
    return {"activation": r1.modified_count, "reset": r2.modified_count}


async def ensure_indexes():
    db = get_db()
    await invalidate_legacy_plaintext_tokens()
    await db.users.create_index("email", unique=True)
    # Bridge tokens are stored as sha256 (`bridge_token_hash`). The legacy
    # unique index on plaintext `bridge_token` is NOT sparse, so once new
    # accounts stop writing the plaintext every doc without it collides on
    # null — replace it with a partial index before any insert can hit it.
    try:
        info = await db.accounts.index_information()
        legacy = info.get("bridge_token_1")
        if legacy and not legacy.get("partialFilterExpression"):
            await db.accounts.drop_index("bridge_token_1")
    except Exception as e:  # noqa: BLE001
        logging.getLogger("seed").warning("bridge_token index migration: %s", e)
    await db.accounts.create_index(
        "bridge_token", unique=True, name="bridge_token_1",
        partialFilterExpression={"bridge_token": {"$type": "string"}})
    await db.accounts.create_index(
        "bridge_token_hash", unique=True, name="bridge_token_hash_1",
        partialFilterExpression={"bridge_token_hash": {"$type": "string"}})
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
    # iter-170 — agent tokens hashed at rest. Migrate any legacy plaintext
    # agent_token → agent_token_hash and DROP the plaintext (idempotent), so a
    # DB read alone can't yield live agent credentials. Index the hash for O(1)
    # auth lookups.
    async for a in db.vps_agents.find(
            {"agent_token": {"$exists": True}},
            {"agent_token": 1}):
        from vps_agent import hash_agent_token
        await db.vps_agents.update_one(
            {"_id": a["_id"]},
            {"$set": {"agent_token_hash": hash_agent_token(a["agent_token"])},
             "$unset": {"agent_token": ""}})
    await db.vps_agents.create_index("agent_token_hash")
    # iter-176 — command_key (HMAC command-signing key) encrypted at rest via
    # secrets_vault. Migrate legacy plaintext and DROP it (idempotent).
    from vps_agent import encrypt_command_key
    async for a in db.vps_agents.find(
            {"command_key": {"$exists": True}}, {"command_key": 1}):
        await db.vps_agents.update_one(
            {"_id": a["_id"]},
            {"$set": {"command_key_enc": encrypt_command_key(a["command_key"])},
             "$unset": {"command_key": ""}})
    # iter-171 (#10) — single-use order authorizations: unique nonce + TTL GC
    await db.order_authorizations.create_index("nonce", unique=True)
    await db.order_authorizations.create_index("issued_at")
    await db.audit_anchors.create_index("seq", unique=True)
    # iter-177 — WebAuthn: single-use challenges auto-expire via TTL;
    # credential ids unique per user.
    await db.webauthn_challenges.create_index("expires_at",
                                              expireAfterSeconds=0)
    await db.webauthn_credentials.create_index(
        [("user_id", 1), ("credential_id", 1)], unique=True)
    await db.signals.create_index([("user_id", 1), ("created_at", -1)])
    await db.trades.create_index([("user_id", 1), ("opened_at", -1)])
    await db.trades.create_index([("user_id", 1), ("status", 1)])
    await db.trades.create_index([("user_id", 1), ("closed_at", -1)])
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
    # Review: scalp model training / model_tasks scan these without an index
    # (collection scan + in-memory sort that fails past Mongo's sort limit).
    await db.scalp_decisions.create_index([("symbol", 1), ("outcome.result", 1),
                                           ("ts_ms", 1)])
    await db.scalp_decisions.create_index([("model_key", 1),
                                           ("outcome.resolved", 1)])
    # Review: per-tenant candle reads (signal features must come from the
    # user's own broker feed) and base-symbol risk queries.
    await db.intraday_candles.create_index([("user_id", 1), ("symbol", 1),
                                            ("updated_at", -1)])
    await db.trades.create_index([("user_id", 1), ("base_symbol", 1),
                                  ("status", 1)])
    # Crypto lifecycle sweep (open/pending exchange trades) + protective
    # order audit trail.
    await db.trades.create_index([("broker_kind", 1), ("status", 1)])
    await db.trades.create_index("client_order_id", sparse=True)
    await db.crypto_protection_audit.create_index([("trade_id", 1), ("at", -1)])
    # Exposure reservations: release looks up trades by their hold id.
    await db.trades.create_index("exposure_reservation.id", sparse=True)
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
    # audit r14 P1-01 — `active` is the ONLY bot enablement field; a legacy
    # `enabled` flag is migrated once and the schema validator refuses it.
    await db.bot_configs.update_many(
        {"enabled": {"$exists": True}, "active": {"$exists": False}},
        [{"$set": {"active": {"$eq": ["$enabled", True]}}}])
    await db.bot_configs.update_many({"enabled": {"$exists": True}}, {"$unset": {"enabled": ""}})
    try:
        await db.command("collMod", "bot_configs", validator={
            "$jsonSchema": {"bsonType": "object", "not": {"required": ["enabled"]}}},
            validationLevel="moderate", validationAction="error")
    except Exception as e:  # noqa: BLE001 — validator is defence-in-depth
        logging.getLogger("seed").warning("bot_configs validator not applied: %s", e)
    # audit r14 P0 — exactly-once execution ledgers
    await db.trigger_fire_events.create_index("event_id", unique=True)
    await db.conditional_triggers.create_index("idem_key", unique=True, sparse=True)
    try:
        await db.model_approval_principals.drop_index("digest_1_principal_id_1")
    except Exception:  # noqa: BLE001 — index may not exist
        pass
    await db.model_approval_principals.create_index([("user_id", 1), ("digest", 1), ("principal_id", 1)], unique=True)
    await db.promotion_publications.create_index("promotion_id", unique=True)
    await db.reconciliation_ledger.create_index([("user_id", 1), ("ledger_seq", 1)], unique=True,
                                                partialFilterExpression={"ledger_seq": {"$exists": True}})
    await db.nl_proposals.create_index([("user_id", 1), ("status", 1)])
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
        # audit v2 P2-03 — one checkout intent per idempotency key (legacy rows
        # without the field are exempt) and one orphan record per paid session.
        await db.payment_transactions.create_index(
            "idempotency_key", unique=True, name="uniq_checkout_idempotency_key",
            partialFilterExpression={"idempotency_key": {"$type": "string"}})
        await db.orphan_payments.create_index("session_id", unique=True)
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
