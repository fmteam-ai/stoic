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

from database import get_db

logger = logging.getLogger(__name__)


async def _nightly_tuning_loop():
    """iter-141 · Hourly check; each user is swept at most once per 24h
    (guard lives in nightly_tuner.sweep_user via quant_tuning_state)."""
    import nightly_tuner
    INTERVAL = int(os.environ.get("NIGHTLY_TUNER_INTERVAL_SEC", "3600"))
    while True:
        try:
            await asyncio.sleep(INTERVAL)
            await nightly_tuner.sweep_all(get_db())
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
            await ai_optimizer.scheduled_sweep()
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
            await auto_heal.sweep_all_users()
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
            await repair_unprotected_positions(get_db())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("protection guard sweep failed")


async def _analytics_loop():
    """Phase F — analytics service: DB-only daily aggregation."""
    from analytics_tasks import run_daily_aggregates
    INTERVAL = int(os.environ.get("ANALYTICS_INTERVAL_SEC", "300"))
    while True:
        try:
            await asyncio.sleep(INTERVAL)
            await run_daily_aggregates(get_db())
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
            await run_model_maintenance(get_db())
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
            await sweep_eod_flatten(get_db())
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
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("stuck-open sync loop error: %s", e)
