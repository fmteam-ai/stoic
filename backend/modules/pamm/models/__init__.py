"""PAMM data model (Phase 3) — 16 collections, broker-authoritative mirror.
No investor balance calculations here; those remain inside the broker."""

COLLECTIONS = [
    "broker_partners", "broker_servers", "pamm_programs",
    "pamm_master_accounts", "pamm_strategies", "pamm_strategy_versions",
    "pamm_allocations", "pamm_nav_snapshots", "pamm_trade_history",
    "pamm_fee_periods", "pamm_reconciliation", "pamm_reports",
    "pamm_audit", "pamm_events", "pamm_notifications", "pamm_health",
]


async def ensure_pamm_setup(db) -> None:
    """Idempotent indexes + sandbox partner seed. Called at startup."""
    await db.broker_partners.create_index("partner_id", unique=True)
    await db.pamm_programs.create_index("program_id", unique=True)
    await db.pamm_programs.create_index("manager_id")
    await db.pamm_allocations.create_index(
        [("program_id", 1), ("investor_id", 1)])
    await db.pamm_nav_snapshots.create_index([("program_id", 1), ("at", -1)])
    await db.pamm_reconciliation.create_index([("program_id", 1), ("at", -1)])
    await db.pamm_events.create_index([("at", -1)])
    await db.pamm_events.create_index("event_key", unique=True, sparse=True)
    await db.pamm_audit.create_index([("program_id", 1), ("at", -1)])
    await db.pamm_trade_history.create_index([("program_id", 1), ("at", -1)])
    await db.pamm_health.create_index([("partner_id", 1), ("at", -1)])
    await db.pamm_join_requests.create_index("request_id", unique=True)
    await db.pamm_join_requests.create_index([("program_id", 1), ("status", 1)])
    await db.pamm_join_requests.create_index([("user_id", 1), ("at", -1)])
    from services.broker_gateway.pamm_api import ensure_sandbox_partner
    await ensure_sandbox_partner(db)
