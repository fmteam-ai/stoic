"""Ops alerting — deduped operational alerts with acknowledgement tracking.

Alerts are raised by the evaluator loop (or any caller via raise_alert),
deduped on an open (unacked) dedup_key, and surfaced through:
  - GET  /api/ops/alerts            (admin session or METRICS_TOKEN)
  - POST /api/ops/alerts/{id}/ack
  - Prometheus: stoic_alerts_unacked{severity}, stoic_alert_oldest_unacked_age_seconds
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from database import get_db

logger = logging.getLogger(__name__)

SEVERITIES = ("info", "warning", "critical")

# Alert kinds whose conditions are re-measured by evaluate_ops_alerts on
# every run — these AUTO-RESOLVE when the condition clears, instead of
# sitting unacked and capping Bot Health until someone clicks acknowledge.
EVALUATOR_KINDS = (
    "ea_heartbeat_stale", "worker_lease_expired", "worker_loop_crashloop",
    "worker_loop_stalled", "outbox_backlog", "outbox_failed",
    "unprotected_positions", "reconciliation_stuck", "pairing_no_heartbeat", "policy_expiring", "policy_expired", "demo_account_reports_real")


def _now():
    return datetime.now(timezone.utc)


async def raise_alert(db, kind: str, severity: str, message: str,
                      dedup_key: str | None = None, meta: dict | None = None,
                      synthetic: bool = False):
    """Insert an alert unless an unacked one with the same dedup_key is open.
    synthetic=True (review P1-7) tags QA/chaos/test alerts so operator
    surfaces can exclude them (scope=real by default)."""
    dedup_key = dedup_key or kind
    if not synthetic:
        # audit v5 P1-3 — auto-tag alerts that reference known test
        # identities so operator surfaces (scope=real) stay clean.
        try:
            from synthetic_data import mentions_synthetic_identity
            synthetic = mentions_synthetic_identity(message, meta)
        except Exception:  # noqa: BLE001
            pass
    now = _now()
    existing = await db.ops_alerts.find_one(
        {"dedup_key": dedup_key, "acked_at": None})
    if existing:
        await db.ops_alerts.update_one(
            {"_id": existing["_id"]},
            {"$set": {"last_seen_at": now, "message": message},
             "$inc": {"occurrences": 1}})
        return None
    res = await db.ops_alerts.insert_one({
        "kind": kind,
        "severity": severity if severity in SEVERITIES else "warning",
        "message": message,
        "dedup_key": dedup_key,
        "meta": meta or {},
        "synthetic": bool(synthetic),
        "created_at": now,
        "last_seen_at": now,
        "occurrences": 1,
        "acked_at": None,
        "acked_by": None,
    })
    logger.warning("OPS ALERT [%s] %s: %s", severity, kind, message)
    if severity == "critical" and not synthetic:
        try:  # iter-152 — critical alerts (stale telemetry, dead workers)
            from guard_alerts import queue_ops_alert_email
            queue_ops_alert_email(db, kind, severity, message, dedup_key)
        except Exception:  # noqa: BLE001 — email must never break alerting
            pass
    return str(res.inserted_id)


def _parse_ts(v):
    try:
        ts = datetime.fromisoformat(str(v))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None


async def evaluate_ops_alerts(db) -> int:
    """Run every production-saving check once; returns alerts raised.
    Tracks the dedup_key of every condition that currently HOLDS; open
    evaluator-managed alerts whose key is absent are auto-resolved."""
    now = _now()
    raised = 0
    active: set = set()

    # 1 · EA heartbeat stale on enabled accounts (recently-active only —
    #     accounts silent for >24h are decommissioned, not incidents)
    hb_max = int(os.environ.get("EA_HEARTBEAT_ALERT_SEC", "300"))
    hb_ceiling = int(os.environ.get("EA_HEARTBEAT_ALERT_MAX_AGE_SEC", "86400"))
    async for a in db.accounts.find(
            {"trading_enabled": True, "dormant": {"$ne": True},  # P0: missing = OFF
             "status": {"$ne": "deleted"}},
            {"label": 1, "last_heartbeat": 1, "user_id": 1, "synthetic": 1}):
        hb = _parse_ts(a.get("last_heartbeat"))
        if hb and hb_max < (now - hb).total_seconds() <= hb_ceiling:
            from synthetic_data import is_synthetic_account
            label = a.get("label") or str(a["_id"])[-6:]
            active.add(f"ea_heartbeat:{a['_id']}")
            if await raise_alert(
                    db, "ea_heartbeat_stale", "critical",
                    f"EA heartbeat for account '{label}' is "
                    f"{int((now - hb).total_seconds())}s old (limit {hb_max}s)",
                    dedup_key=f"ea_heartbeat:{a['_id']}",
                    synthetic=is_synthetic_account(a)):
                raised += 1

    # 2 · worker lease expired / loop crashlooping / loop stalled
    # Leases expired beyond the ceiling are decommissioned workers (e.g.
    # preview/single-process mode), not incidents — otherwise the evaluator
    # re-raises the same critical alert forever after every acknowledge.
    lease_ceiling = int(os.environ.get("WORKER_LEASE_ALERT_MAX_AGE_SEC", "86400"))
    async for w in db.worker_leases.find({}):
        name = str(w.get("_id"))
        exp = _parse_ts(w.get("expires_at"))
        if exp and exp < now:
            if (now - exp).total_seconds() > lease_ceiling:
                continue  # abandoned lease — decommissioned, skip all checks
            active.add(f"worker_lease:{name}")
            if await raise_alert(
                    db, "worker_lease_expired", "critical",
                    f"Worker '{name}' lease expired at {w.get('expires_at')}",
                    dedup_key=f"worker_lease:{name}"):
                raised += 1
        for ln, st in (w.get("loops") or {}).items():
            if (st or {}).get("consecutive_failures", 0) >= 3:
                active.add(f"crashloop:{name}:{ln}")
                if await raise_alert(
                        db, "worker_loop_crashloop", "critical",
                        f"Loop '{ln}' in worker '{name}' has "
                        f"{st['consecutive_failures']} consecutive failures",
                        dedup_key=f"crashloop:{name}:{ln}"):
                    raised += 1
            # a loop that stops iterating while its coroutine stays alive
            ivl = (st or {}).get("expected_interval_sec")
            done = _parse_ts((st or {}).get("last_iteration_completed_at"))
            if ivl and done and exp and exp >= now:
                stall_after = max(3 * int(ivl), 120)
                if (now - done).total_seconds() > stall_after:
                    active.add(f"stalled:{name}:{ln}")
                    if await raise_alert(
                            db, "worker_loop_stalled", "critical",
                            f"Loop '{ln}' in worker '{name}' made no progress "
                            f"for {int((now - done).total_seconds())}s "
                            f"(expected every {ivl}s)",
                            dedup_key=f"stalled:{name}:{ln}"):
                        raised += 1

    # 3 · outbox backlog aging / failed events
    backlog_cutoff = (now - timedelta(
        seconds=int(os.environ.get("OUTBOX_AGE_ALERT_SEC", "300")))).isoformat()
    aged = await db.outbox.count_documents(
        {"state": "pending", "created_at": {"$lt": backlog_cutoff}})
    if aged:
        active.add("outbox_backlog")
        if await raise_alert(db, "outbox_backlog", "warning",
                             f"{aged} outbox event(s) pending for >5m",
                             dedup_key="outbox_backlog"):
            raised += 1
    failed = await db.outbox.count_documents({"state": "failed"})
    if failed:
        active.add("outbox_failed")
        if await raise_alert(db, "outbox_failed", "warning",
                             f"{failed} outbox event(s) in failed state",
                             dedup_key="outbox_failed"):
            raised += 1

    # 4 · open positions without broker-confirmed protection
    from protection_guard import unprotected_open_query
    prot_cutoff = (now - timedelta(
        seconds=int(os.environ.get("UNPROTECTED_ALERT_SEC", "180")))).isoformat()
    upq = unprotected_open_query({"opened_at": {"$lt": prot_cutoff}})
    unprotected = await db.trades.count_documents(upq)
    if unprotected:
        active.add("unprotected_positions")
        samples = await db.trades.find(
            upq, {"symbol": 1, "mt5_ticket": 1}).sort(
            "opened_at", 1).to_list(3)
        ids = ", ".join(
            f"{str(d.get('symbol') or '?').rstrip('#')}"
            f"#{d.get('mt5_ticket') or str(d['_id'])[-6:]}"
            for d in samples)
        more = ", …" if unprotected > len(samples) else ""
        if await raise_alert(
                db, "unprotected_positions", "critical",
                f"{unprotected} open position(s) without a broker-confirmed "
                f"stop for >3 minutes ({ids}{more})",
                dedup_key="unprotected_positions"):
            raised += 1

    # 5 · broker-accepted orders stuck unresolved (reconciliation lag)
    stale_cutoff = (now - timedelta(minutes=5)).isoformat()
    stuck = await db.trades.count_documents(
        {"status": "pending",
         "submission_state": "broker_accepted_unresolved",
         "updated_at": {"$lt": stale_cutoff}})
    if stuck:
        active.add("reconciliation_stuck")
        if await raise_alert(
                db, "reconciliation_stuck", "critical",
                f"{stuck} broker-accepted order(s) unresolved for >5m",
                dedup_key="reconciliation_stuck"):
            raised += 1

    # 5c · A16-4 — signed (demo) policy lifetime: Telegram reminder 3 days before, critical alert once expired
    try:
        import policy_expiry_alerts
        _pe_active, _pe_raised = await policy_expiry_alerts.evaluate(db, now, raise_alert=raise_alert)
        active |= _pe_active
        raised += _pe_raised
    except Exception as e:  # noqa: BLE001
        logger.warning("policy expiry alert evaluation failed: %s", type(e).__name__)
        try:
            active |= {a["dedup_key"] async for a in db.ops_alerts.find(
                {"kind": {"$in": ["policy_expiring", "policy_expired"]}, "acked_at": None}, {"dedup_key": 1}) if a.get("dedup_key")}
        except Exception as e2:  # noqa: BLE001
            logger.warning("policy expiry keep-open failed: %s", type(e2).__name__)

    # 5d · A16-5 — a DEMO-attested account whose broker reports real/contest money (attestation void) → loud
    try:
        import demo_mode_alerts
        _dm_active, _dm_raised = await demo_mode_alerts.evaluate(db, now, raise_alert=raise_alert)
        active |= _dm_active
        raised += _dm_raised
    except Exception as e:  # noqa: BLE001
        logger.warning("demo mode alert evaluation failed: %s", type(e).__name__)
        try:
            active |= {a["dedup_key"] async for a in db.ops_alerts.find(
                {"kind": "demo_account_reports_real", "acked_at": None}, {"dedup_key": 1}) if a.get("dedup_key")}
        except Exception as e2:  # noqa: BLE001
            logger.warning("demo mode keep-open failed: %s", type(e2).__name__)

    # 5b · paired VPS terminal silent since the installer ran (WebRequest / EA attach / AutoTrading) —
    #      ops alert + security Telegram push, recovery announced when the first heartbeat lands
    try:
        import pairing_alerts
        _pa_active, _pa_raised = await pairing_alerts.evaluate(db, now, raise_alert=raise_alert)
        active |= _pa_active
        raised += _pa_raised
    except Exception as e:  # noqa: BLE001 — one failing detector must not stop the others
        logger.warning("pairing alert evaluation failed: %s", type(e).__name__)
        # N106-5 — a failed check is not a recovery: keep its open alerts out of the auto-resolve
        # below, otherwise they close now and are raised (and pushed to Telegram) again next cycle.
        try:
            active |= {a["dedup_key"] async for a in db.ops_alerts.find(
                {"kind": "pairing_no_heartbeat", "acked_at": None}, {"dedup_key": 1}) if a.get("dedup_key")}
        except Exception as e2:  # noqa: BLE001
            logger.warning("pairing alert keep-open failed: %s", type(e2).__name__)

    # 6 · AUTO-RESOLVE — evaluator-managed alerts whose condition no longer
    # holds are closed automatically (acked_by system:auto-resolved) so a
    # transient blip (terminal reconnects, backlog drains, stop confirmed)
    # never caps Bot Health until a human clicks acknowledge.
    ts = _now()
    res = await db.ops_alerts.update_many(
        {"acked_at": None, "kind": {"$in": list(EVALUATOR_KINDS)},
         "dedup_key": {"$nin": sorted(active)}},
        {"$set": {"acked_at": ts, "acked_by": "system:auto-resolved",
                  "auto_resolved": True, "resolved_at": ts}})
    if res.modified_count:
        logger.info("auto-resolved %d ops alert(s) — condition cleared",
                    res.modified_count)

    return raised


async def _ops_alert_loop():
    """Periodic evaluator — runs in-process or inside the reconciliation worker."""
    interval = int(os.environ.get("OPS_ALERT_INTERVAL_SEC", "60"))
    from workers.base import record_progress
    while True:
        try:
            await asyncio.sleep(interval)
            t0 = _now()
            raised = await evaluate_ops_alerts(get_db())
            record_progress("_ops_alert_loop", processed=1 + raised,
                            started_at=t0, interval_sec=interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("ops alert loop error: %s", e)
