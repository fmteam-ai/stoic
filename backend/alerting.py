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


def _now():
    return datetime.now(timezone.utc)


async def raise_alert(db, kind: str, severity: str, message: str,
                      dedup_key: str | None = None, meta: dict | None = None):
    """Insert an alert unless an unacked one with the same dedup_key is open."""
    dedup_key = dedup_key or kind
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
        "created_at": now,
        "last_seen_at": now,
        "occurrences": 1,
        "acked_at": None,
        "acked_by": None,
    })
    logger.warning("OPS ALERT [%s] %s: %s", severity, kind, message)
    if severity == "critical":
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
    """Run every production-saving check once; returns alerts raised."""
    now = _now()
    raised = 0

    # 1 · EA heartbeat stale on enabled accounts (recently-active only —
    #     accounts silent for >24h are decommissioned, not incidents)
    hb_max = int(os.environ.get("EA_HEARTBEAT_ALERT_SEC", "300"))
    hb_ceiling = int(os.environ.get("EA_HEARTBEAT_ALERT_MAX_AGE_SEC", "86400"))
    async for a in db.accounts.find(
            {"trading_enabled": {"$ne": False}, "dormant": {"$ne": True},
             "status": {"$ne": "deleted"}},
            {"label": 1, "last_heartbeat": 1}):
        hb = _parse_ts(a.get("last_heartbeat"))
        if hb and hb_max < (now - hb).total_seconds() <= hb_ceiling:
            label = a.get("label") or str(a["_id"])[-6:]
            if await raise_alert(
                    db, "ea_heartbeat_stale", "critical",
                    f"EA heartbeat for account '{label}' is "
                    f"{int((now - hb).total_seconds())}s old (limit {hb_max}s)",
                    dedup_key=f"ea_heartbeat:{a['_id']}"):
                raised += 1

    # 2 · worker lease expired / loop crashlooping / loop stalled
    async for w in db.worker_leases.find({}):
        name = str(w.get("_id"))
        exp = _parse_ts(w.get("expires_at"))
        if exp and exp < now:
            if await raise_alert(
                    db, "worker_lease_expired", "critical",
                    f"Worker '{name}' lease expired at {w.get('expires_at')}",
                    dedup_key=f"worker_lease:{name}"):
                raised += 1
        for ln, st in (w.get("loops") or {}).items():
            if (st or {}).get("consecutive_failures", 0) >= 3:
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
        if await raise_alert(db, "outbox_backlog", "warning",
                             f"{aged} outbox event(s) pending for >5m",
                             dedup_key="outbox_backlog"):
            raised += 1
    failed = await db.outbox.count_documents({"state": "failed"})
    if failed:
        if await raise_alert(db, "outbox_failed", "warning",
                             f"{failed} outbox event(s) in failed state",
                             dedup_key="outbox_failed"):
            raised += 1

    # 4 · open positions without broker-confirmed protection
    prot_cutoff = (now - timedelta(
        seconds=int(os.environ.get("UNPROTECTED_ALERT_SEC", "180")))).isoformat()
    unprotected = await db.trades.count_documents(
        {"status": "open", "opened_at": {"$lt": prot_cutoff},
         "$or": [{"stop_loss": {"$in": [None, 0]}},
                 {"lifecycle_state": {"$in": ["FILLED_UNPROTECTED",
                                              "PROTECTION_REQUESTED"]}}]})
    if unprotected:
        if await raise_alert(
                db, "unprotected_positions", "critical",
                f"{unprotected} open position(s) without a broker-confirmed "
                f"stop for >3 minutes",
                dedup_key="unprotected_positions"):
            raised += 1

    # 5 · broker-accepted orders stuck unresolved (reconciliation lag)
    stale_cutoff = (now - timedelta(minutes=5)).isoformat()
    stuck = await db.trades.count_documents(
        {"status": "pending",
         "submission_state": "broker_accepted_unresolved",
         "updated_at": {"$lt": stale_cutoff}})
    if stuck:
        if await raise_alert(
                db, "reconciliation_stuck", "critical",
                f"{stuck} broker-accepted order(s) unresolved for >5m",
                dedup_key="reconciliation_stuck"):
            raised += 1

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
