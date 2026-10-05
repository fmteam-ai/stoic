"""Perpetual background loops (review item 11 — extracted from server.py).

These run either in-process inside the API (default, preview environment) or
in dedicated worker processes (production):
    python -m workers.trading         # bot runner + trade manager + warmer
    python -m workers.reconciliation  # auto-heal, stuck-sync, scalp sweeps, EOD
    python -m workers.tuning          # optimizer + nightly tuner
Set BACKGROUND_WORKERS_IN_PROCESS=false on the API service when the workers
run externally, so API restarts/deploys never interrupt trading loops.
"""
import asyncio
import logging
import os
import socket
from datetime import datetime, timezone, timedelta

from database import get_db
from workers.base import record_progress

logger = logging.getLogger(__name__)


async def _pamm_sweep_loop():
    """iter-188 · PAMM Phases 9/10 — automatic risk sweeps + broker
    heartbeat every PAMM_SWEEP_INTERVAL_SEC (default 60s)."""
    INTERVAL = int(os.environ.get("PAMM_SWEEP_INTERVAL_SEC", "60"))
    while True:
        try:
            await asyncio.sleep(INTERVAL)
            t0 = datetime.now(timezone.utc)
            from modules.pamm.sweep import sweep_once
            res = await sweep_once(get_db())
            record_progress("_pamm_sweep_loop",
                            processed=res.get("programs_checked", 0),
                            started_at=t0, interval_sec=INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("pamm sweep failed: %s", e)


async def _nightly_tuning_loop():
    """iter-141 · Hourly check; each user is swept at most once per 24h
    (guard lives in nightly_tuner.sweep_user via quant_tuning_state)."""
    import nightly_tuner
    INTERVAL = int(os.environ.get("NIGHTLY_TUNER_INTERVAL_SEC", "3600"))
    while True:
        try:
            await asyncio.sleep(INTERVAL)
            t0 = datetime.now(timezone.utc)
            await nightly_tuner.sweep_all(get_db())
            record_progress("_nightly_tuning_loop", processed=1,
                            started_at=t0, interval_sec=INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("nightly tuner sweep failed: %s", e)


async def _optimizer_loop():
    """Hourly sweep — AI Strategy Optimizer re-analyzes each ACTIVE bot scope
    at most once per 24h (see ai_optimizer.scheduled_sweep guards)."""
    import ai_optimizer
    INTERVAL = int(os.environ.get("OPTIMIZER_SWEEP_INTERVAL_SEC", "3600"))
    while True:
        try:
            await asyncio.sleep(INTERVAL)
            t0 = datetime.now(timezone.utc)
            await ai_optimizer.scheduled_sweep()
            record_progress("_optimizer_loop", processed=1,
                            started_at=t0, interval_sec=INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("optimizer sweep failed: %s", e)


async def _auto_heal_loop():
    """Background sweep — every 5 min, runs all opted-in users through the
    safe-fix cascade (auto_heal.sweep_all_users)."""
    import auto_heal
    INTERVAL = int(os.environ.get("AUTO_HEAL_INTERVAL_SEC", "300"))
    while True:
        try:
            await asyncio.sleep(INTERVAL)
            t0 = datetime.now(timezone.utc)
            await auto_heal.sweep_all_users()
            record_progress("_auto_heal_loop", processed=1,
                            started_at=t0, interval_sec=INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("auto-heal sweep failed: %s", e)


async def _protection_guard_loop():
    """Phase F — dedicated protection service: an unprotected position
    cannot wait on any other sweep's schedule."""
    from protection_guard import repair_unprotected_positions
    PROT = int(os.environ.get("SCALP_PROTECTION_SWEEP_SEC", "10"))
    while True:
        try:
            await asyncio.sleep(PROT)
            t0 = datetime.now(timezone.utc)
            await repair_unprotected_positions(get_db())
            record_progress("_protection_guard_loop", processed=1,
                            started_at=t0, interval_sec=PROT)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("protection guard sweep failed")


async def _heartbeat_watch_loop():
    """iter-173 — EA heartbeat / verification outage alerts (transition
    based; Telegram + owner email + in-app alert)."""
    from heartbeat_watch import check_once
    INTERVAL = int(os.environ.get("HEARTBEAT_WATCH_INTERVAL_SEC", "60"))
    while True:
        try:
            await asyncio.sleep(INTERVAL)
            t0 = datetime.now(timezone.utc)
            await check_once(get_db())
            record_progress("_heartbeat_watch_loop", processed=1,
                            started_at=t0, interval_sec=INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("heartbeat watch sweep failed")


async def _analytics_loop():
    """Phase F — analytics service: DB-only daily aggregation + the
    idempotent health repairs (moved out of GET /bot/health-score)."""
    from analytics_tasks import run_daily_aggregates
    from health_repairs import run_all_users as run_health_repairs_all
    INTERVAL = int(os.environ.get("ANALYTICS_INTERVAL_SEC", "300"))
    REPAIR_EVERY = int(os.environ.get("HEALTH_REPAIR_INTERVAL_SEC", "120"))
    last_repair = 0.0
    while True:
        try:
            await asyncio.sleep(min(INTERVAL, REPAIR_EVERY))
            import time as _t
            if _t.time() - last_repair >= REPAIR_EVERY:
                await run_health_repairs_all(get_db())
                last_repair = _t.time()
                record_progress("_health_repair_loop", processed=1,
                                started_at=datetime.now(timezone.utc),
                                interval_sec=REPAIR_EVERY)
            if _t.time() - getattr(_analytics_loop, "_last", 0) < INTERVAL:
                continue
            _analytics_loop._last = _t.time()
            t0 = datetime.now(timezone.utc)
            await run_daily_aggregates(get_db())
            record_progress("_analytics_loop", processed=1,
                            started_at=t0, interval_sec=INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("analytics aggregation failed")


async def _model_maintenance_loop():
    """Phase F — model service: scheduled retrains + registry audit."""
    from model_tasks import run_model_maintenance
    INTERVAL = int(os.environ.get("MODEL_MAINT_INTERVAL_SEC", "21600"))
    while True:
        try:
            await asyncio.sleep(INTERVAL)
            t0 = datetime.now(timezone.utc)
            await run_model_maintenance(get_db())
            record_progress("_model_maintenance_loop", processed=1,
                            started_at=t0, interval_sec=INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("model maintenance failed")


async def _scalp_reconcile_loop():
    """Round 9 cadences — one scheduler, three sweep frequencies:
      • financial pending-deal sweep: every 45s
      • full invariant + durable-state sweep: every 300s
    (Phase F: the protection safety sweep moved to _protection_guard_loop —
    its own service — so this loop only reconciles.)"""
    from scalp.engine import (recover_pending_deals, sweep_submission_slots,
                              verify_account_invariants,
                              verify_durable_invariants, _runners)
    PROT = int(os.environ.get("SCALP_PROTECTION_SWEEP_SEC", "10"))
    FIN = int(os.environ.get("SCALP_RECONCILE_INTERVAL_SEC", "45"))
    FULL = int(os.environ.get("SCALP_INVARIANT_SWEEP_SEC", "300"))
    # Round 17 — submission-slot lifecycle sweep at ~lease/3 so pending
    # orders keep their leases renewed and terminal/orphan slots free up.
    SLOT = int(os.environ.get("SCALP_SLOT_SWEEP_SEC", "30"))
    # Round 12 item 8 — independent monotonic deadlines per sweep: exact
    # cadence regardless of interval ratios, no drift when a sweep is slow.
    from time import monotonic
    next_fin = monotonic() + FIN
    next_full = monotonic() + FULL
    next_slot = monotonic() + SLOT
    while True:
        try:
            await asyncio.sleep(PROT)
            t0 = datetime.now(timezone.utc)
            now_m = monotonic()
            if now_m >= next_slot:
                next_slot = now_m + SLOT
                await sweep_submission_slots(get_db())
            if now_m >= next_fin:
                next_fin = now_m + FIN
                await recover_pending_deals(get_db())
                # review item 2 — resolve stale provisional risk reservations
                from scalp.risk_reservations import sweep_stale
                await sweep_stale(get_db())
                # A13 P0-01 — crypto execution truth: orders by client id, protection, balances
                try:
                    from crypto_bridge.crypto_execution import reconcile_all
                    await reconcile_all(get_db())
                except Exception as e:  # noqa: BLE001
                    logger.warning("crypto reconcile sweep failed: %s", type(e).__name__)
                # Phase A — transactional-outbox relay: publish any critical
                # events a crash left pending (producers awaited the insert).
                from scalp.outbox import relay_once
                await relay_once(get_db())
            if now_m >= next_full:
                next_full = now_m + FULL
                try:
                    await verify_durable_invariants(get_db())
                except Exception as e:  # noqa: BLE001
                    from scalp.engine import _invariant_scan
                    _invariant_scan["last_error"] = str(e)
                    raise
                for account_id in {k.split(":")[0] for k in list(_runners)}:
                    verify_account_invariants(account_id)
            record_progress("_scalp_reconcile_loop", processed=1,
                            started_at=t0, interval_sec=PROT)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("scalp reconcile sweep failed: %s", e)


async def _eod_flatten_loop():
    """iter-53 · EOD flatten — every 60s, close all open intraday auto
    entries on accounts inside their broker-time flatten window (23:15-23:40
    broker time) so nothing is held through the daily rollover."""
    from eod_flatten import sweep_eod_flatten
    while True:
        try:
            await asyncio.sleep(60)
            t0 = datetime.now(timezone.utc)
            await sweep_eod_flatten(get_db())
            record_progress("_eod_flatten_loop", processed=1,
                            started_at=t0, interval_sec=60)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("EOD flatten sweep failed: %s", e)


async def _stuck_open_sync_loop():
    """iter-94 · Always-on stuck-open trade sync (no opt-in required).

    Every N seconds (default 180) find users whose DB has trades in
    status='open' with opened_at > 10 minutes ago, and force-reconcile them
    against the broker's live ticket list. This is what previously required
    a manual click on the "SYNC WITH BROKER" button — now it runs on its
    own so trades that lost their `OnTradeTransaction` close event never
    sit ghost-open indefinitely.

    iter-95 · Also auto-backfill closed trades missing exit_price. When a
    trade is marked status=closed but exit_price=None > 10 min after
    closed_at AND the broker no longer holds the ticket, best-effort fill
    exit_price = entry_price (breakeven placeholder) so the UI stops
    showing dashes forever. This covers the auto-deleverage / slippage-veto
    close paths where the EA never returns the actual fill price/pnl.

    Safe: reconcile_user only closes trades whose ticket is missing from
    the broker's current heartbeat AND whose opened_at is > 45s ago. Fresh
    trades in the fill window are never touched.
    """
    INTERVAL = int(os.environ.get("STUCK_OPEN_SYNC_INTERVAL_SEC", "180"))
    STALE_MINUTES = int(os.environ.get("STUCK_OPEN_STALE_MINUTES", "10"))
    EXIT_BACKFILL_MINUTES = int(os.environ.get("EXIT_PRICE_BACKFILL_MINUTES", "10"))
    while True:
        try:
            await asyncio.sleep(INTERVAL)
            db = get_db()
            from datetime import datetime, timezone, timedelta as _td
            cutoff_iso = (datetime.now(timezone.utc) - _td(minutes=STALE_MINUTES)).isoformat()
            # Distinct users with a stuck-open trade
            user_ids = await db.trades.distinct("user_id", {
                "status": "open",
                "$or": [
                    {"opened_at": {"$lt": cutoff_iso}},
                    {"opened_at": None},
                ],
            })
            if user_ids:
                from trade_reconciler import reconcile_user
                closed_total = 0
                for uid in user_ids:
                    try:
                        result = await reconcile_user(uid, force=True)
                        n = (result or {}).get("total_closed", 0)
                        if n > 0:
                            closed_total += n
                            logger.info(
                                "stuck-open sync closed %s trade(s) for user=%s",
                                n, uid,
                            )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "stuck-open sync failed for user=%s: %s", uid, e,
                        )
                if closed_total:
                    logger.info(
                        "stuck-open sync total: %s trade(s) closed across %s user(s)",
                        closed_total, len(user_ids),
                    )

            # iter-95: exit-price backfill sweep
            exit_cutoff = (datetime.now(timezone.utc) - _td(minutes=EXIT_BACKFILL_MINUTES)).isoformat()
            missing_cursor = db.trades.find({
                "status": "closed",
                "exit_price": None,
                "closed_at": {"$lt": exit_cutoff},
                "ghost_acknowledged": {"$ne": True},
                "mt5_ticket": {"$ne": None},
            })
            backfilled = 0
            async for t in missing_cursor:
                # Confirm broker no longer holds this ticket. If open_tickets
                # is populated, use it; otherwise fall back to open_positions
                # count being 0.
                try:
                    from bson import ObjectId as _OID
                    acc = await db.accounts.find_one({"_id": _OID(t["account_id"])})
                except Exception:
                    acc = None
                if not acc:
                    continue
                open_tix = acc.get("open_tickets")
                if isinstance(open_tix, list):
                    still_open_at_broker = int(t.get("mt5_ticket") or -1) in {
                        int(x) for x in open_tix if x is not None
                    }
                elif open_tix is None:
                    still_open_at_broker = (acc.get("open_positions") or 0) > 0
                else:
                    still_open_at_broker = True
                if still_open_at_broker:
                    continue
                # Round 18 (audit fix): NEVER fabricate a financial fill.
                # exit_price stays null; the entry price is stored ONLY as
                # a display estimate, and the row is excluded from training
                # and financial metrics until broker deal history resolves
                # it (deal-history sweep overwrites with exact figures).
                await db.trades.update_one(
                    {"_id": t["_id"]},
                    {"$set": {
                        "outcome_status": "financially_unresolved",
                        "display_exit_price_estimate":
                            float(t.get("entry_price") or 0),
                        "exclude_from_training": True,
                        "exclude_from_financial_metrics": True,
                        "pnl_unknown": t.get("pnl") is None,
                        "exit_price_source": "unresolved_broker_missing",
                        "exit_unresolved_at": datetime.now(timezone.utc).isoformat(),
                        "ghost_acknowledged": True,
                    }},
                )
                backfilled += 1
                logger.info(
                    "trade=%s ticket=%s user=%s marked financially_unresolved "
                    "(broker no longer holds; no fabricated exit price)",
                    t.get("_id"), t.get("mt5_ticket"), t.get("user_id"),
                )
            if backfilled:
                logger.info(
                    "exit-price backfill sweep: %s trade(s) auto-filled", backfilled,
                )
            record_progress("_stuck_open_sync_loop", processed=1,
                            started_at=None, interval_sec=INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("stuck-open sync loop error: %s", e)


async def _mode_guardian_loop():
    """Correction #6 — every 5 min: health-sample every user running a live
    mode and auto-demote authority when health deteriorates."""
    from auto_demotion import sweep_user
    INTERVAL = int(os.environ.get("MODE_GUARDIAN_INTERVAL_SEC", "300"))
    while True:
        try:
            await asyncio.sleep(INTERVAL)
            t0 = datetime.now(timezone.utc)
            db = get_db()
            users = await db.bot_configs.distinct(
                "user_id", {"active": True, "operational_mode":
                            {"$in": ["autonomous_live", "supervised_live"]}})
            for uid in users:
                try:
                    await sweep_user(db, uid)
                except Exception as e:  # noqa: BLE001
                    logger.warning("mode guardian sweep failed user=%s: %s",
                                   uid, e)
            record_progress("_mode_guardian_loop", processed=len(users),
                            started_at=t0, interval_sec=INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("mode guardian loop error: %s", e)


def _rss_mb() -> float | None:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


def _renewal_email_html(name: str, plan_label: str, ends_on: str, days: int) -> str:
    day_word = f"{days} day{'s' if days > 1 else ''}"
    return f"""
<div style="background:#0A0A0A;color:#FAFAFA;font-family:'Courier New',monospace;padding:32px;max-width:560px;margin:auto;border:1px solid #1F1F1F">
  <div style="color:#00FF41;font-size:20px;font-weight:bold;letter-spacing:4px;margin-bottom:24px">STOIC</div>
  <div style="font-size:16px;margin-bottom:16px">Hi {name},</div>
  <div style="font-size:14px;color:#A1A1AA;line-height:1.6;margin-bottom:16px">
    Your prepaid <span style="color:#FAFAFA">{plan_label}</span> access pass ends in
    <span style="color:#FFB000;font-weight:bold">{day_word}</span> — on {ends_on}.
  </div>
  <div style="font-size:14px;color:#A1A1AA;line-height:1.6;margin-bottom:24px">
    When it lapses, live trading pauses (paper trading keeps working).
    We never auto-charge you — renew any time from the Subscription page.
  </div>
  <a href="https://stoicaibot.com/subscription"
     style="display:inline-block;background:#00FF41;color:#000;padding:10px 24px;font-size:13px;letter-spacing:2px;text-decoration:none;font-weight:bold">RENEW MY PLAN</a>
  <div style="font-size:11px;color:#52525B;margin-top:24px">
    STOIC is software, not a broker. Your funds stay with your broker.
  </div>
</div>"""


async def renewal_reminder_sweep(db, now=None):
    """Send prepaid renewal reminders 7 days and 1 day before a pass lapses:
    in-app notification + Resend email, both deduped per valid_until."""
    now = now or datetime.now(timezone.utc)
    sent = 0
    for days, flag in ((7, "reminder_7d_for"), (1, "reminder_1d_for")):
        horizon = (now + timedelta(days=days)).isoformat()
        cursor = db.subscriptions.find({
            "valid_until": {"$gt": now.isoformat(), "$lt": horizon},
            "current_plan_id": {"$nin": [None, "admin_grandfather"]},
        })
        async for sub in cursor:
            vu = sub.get("valid_until")
            if sub.get(flag) == vu:
                continue  # already reminded for this expiry
            await db.notifications.insert_one({
                "user_id": sub["user_id"],
                "type": "renewal_reminder",
                "title": f"Your STOIC plan lapses in {days} day{'s' if days > 1 else ''}",
                "message": ("Your prepaid access pass "
                            f"({sub.get('current_plan_id')}) ends on "
                            f"{vu[:10]}. Renew from the Subscription "
                            "page to keep the bot trading — we never "
                            "auto-charge you."),
                "read": False,
                "created_at": now.isoformat(),
            })
            try:
                import email_sender
                if email_sender.is_configured():
                    from bson import ObjectId
                    try:
                        u = await db.users.find_one(
                            {"_id": ObjectId(sub["user_id"])},
                            {"email": 1, "name": 1})
                    except Exception:
                        u = None
                    if u and u.get("email"):
                        plan_label = (sub.get("current_plan_id") or "plan").replace("_", " ").title()
                        await email_sender.send_email(
                            recipient=u["email"],
                            subject=f"Your STOIC plan ends in {days} day{'s' if days > 1 else ''} — renew to keep trading",
                            html=_renewal_email_html(
                                u.get("name") or "trader", plan_label,
                                (vu or "")[:10], days),
                        )
            except Exception:
                logger.warning("renewal reminder email failed for user=%s",
                               sub.get("user_id"))
            await db.subscriptions.update_one(
                {"_id": sub["_id"]}, {"$set": {flag: vu}})
            sent += 1
    return sent


async def _billing_loop():
    """iter-122 — billing correctness daemon: retries pending affiliate
    commission outbox entries and sends prepaid renewal reminders 7 days
    and 1 day before a pass lapses (in-app notification + email, deduped
    per valid_until)."""
    await asyncio.sleep(45)
    while True:
        try:
            db = get_db()
            from affiliate_service import process_affiliate_outbox
            await process_affiliate_outbox(db)
            await renewal_reminder_sweep(db)
        except Exception:
            logger.exception("billing loop iteration failed")
        await asyncio.sleep(3600)


async def _soak_sampler_loop():
    """Phase 2.2 — long-soak telemetry: RSS, worker liveness, heartbeat age
    sampled every SOAK_SAMPLE_INTERVAL_SEC into ops_soak_samples (30d TTL)."""
    INTERVAL = int(os.environ.get("SOAK_SAMPLE_INTERVAL_SEC", "600"))
    indexed = False
    first = True
    while True:
        try:
            # r15 P2-01 — sample at start-up too, so every (re)started process
            # announces its identity/build instead of staying invisible for INTERVAL
            await asyncio.sleep(5 if first else INTERVAL)
            first = False
            db = get_db()
            if not indexed:
                await db.ops_soak_samples.create_index(
                    "at", expireAfterSeconds=45 * 24 * 3600)
                indexed = True
            now = datetime.now(timezone.utc)
            total = alive = 0
            async for w in db.worker_leases.find({}, {"expires_at": 1}):
                exp = w.get("expires_at")
                if isinstance(exp, str):
                    try:
                        exp = datetime.fromisoformat(exp)
                    except ValueError:
                        exp = None
                if exp is not None and exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                total += 1
                if exp and exp > now:
                    alive += 1
            hb_ages = []
            async for a in db.accounts.find(
                    {"last_heartbeat": {"$ne": None},
                     "status": {"$ne": "deleted"}}, {"last_heartbeat": 1}):
                hb = a.get("last_heartbeat")
                if isinstance(hb, str):
                    try:
                        hb = datetime.fromisoformat(hb)
                    except ValueError:
                        continue
                if hb is not None and hb.tzinfo is None:
                    hb = hb.replace(tzinfo=timezone.utc)
                if hb:
                    hb_ages.append((now - hb).total_seconds())
            from modules.pamm.strategy_guard import GIT_COMMIT as _build
            await db.ops_soak_samples.insert_one({
                "at": now, "rss_mb": _rss_mb(),
                # audit r14 P2-01 — every sample names its process so replicas
                # never mix and a missing series is detectable.
                "service": os.environ.get("STOIC_SERVICE_NAME") or "api",
                "host": socket.gethostname(), "pid": os.getpid(),
                "build": (_build or "unknown")[:12],
                "workers_alive": alive, "workers_total": total,
                "hb_age_min_s": round(min(hb_ages)) if hb_ages else None,
                "hb_age_max_s": round(max(hb_ages)) if hb_ages else None})
            record_progress("_soak_sampler_loop", processed=1,
                            started_at=None, interval_sec=INTERVAL)
            try:
                from soak_memory_watch import sweep as memory_sweep
                await memory_sweep(db)
            except Exception as e:  # noqa: BLE001
                logger.warning("soak memory watch error: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("soak sampler loop error: %s", e)


async def _soak_tracker_loop():
    """iter-159 — soak auto-start on release change, daily checkpoint
    reminders + end-of-day safety-net checkpoint, and release-canary
    divergence evaluation. Every SOAK_TRACKER_INTERVAL_SEC (30 min)."""
    INTERVAL = int(os.environ.get("SOAK_TRACKER_INTERVAL_SEC", "1800"))
    while True:
        try:
            await asyncio.sleep(INTERVAL)
            t0 = datetime.now(timezone.utc)
            from soak_campaign import tracker_sweep
            await tracker_sweep(get_db())
            from release_canary import evaluate as canary_evaluate
            await canary_evaluate(get_db())
            record_progress("_soak_tracker_loop", processed=1,
                            started_at=t0, interval_sec=INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("soak tracker sweep failed: %s", e)
