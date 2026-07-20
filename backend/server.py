from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / ".env")

import os
import asyncio
import logging
from fastapi import FastAPI, APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from starlette.middleware.cors import CORSMiddleware
from bson import ObjectId

from database import close_client, get_db
from seed import seed_admin, ensure_indexes
from auth import decode_token
from ws_manager import manager as ws_manager
import bot_runner
import trade_manager
import warmer

# Routers
from routes.admin_routes import router as admin_router
from routes.migration_routes import router as migration_router
from routes.setup_routes import router as setup_router
from routes.auth_routes import router as auth_router
from routes.market_routes import router as market_router
from routes.bot_routes import router as bot_router
from routes.signal_routes import router as signal_router
from routes.account_routes import router as account_router
from routes.trade_routes import router as trade_router
from routes.bridge_routes import router as bridge_router
from routes.sentiment_routes import router as sentiment_router
from routes.calendar_routes import router as calendar_router
from routes.panic_routes import router as panic_router
from routes.nl_routes import router as nl_router
from routes.copilot_routes import router as copilot_router
from routes.bugs_routes import router as bugs_router
from routes.subscription_routes import router as subscription_router
from routes.affiliate_routes import router as affiliate_router
from routes.notification_routes import router as notification_router
from routes.telegram_routes import router as telegram_router
from routes.posture_routes import router as posture_router
from routes.rl_routes import router as rl_router
from routes.ml_routes import router as ml_router
from routes.architecture_routes import router as architecture_router
from routes.analytics_routes import router as analytics_router
from routes.entitlement_routes import router as entitlement_router
from routes.agent_routes import router as agent_router
from routes.partner_routes import router as partner_router
from routes.integrity_routes import router as integrity_router
from routes.diagnostic_routes import router as diagnostic_router
from routes.safety_blocks_routes import router as safety_blocks_router
from routes.macro_routes import router as macro_router
from routes.strategies_routes import router as strategies_router
from routes.portfolio_routes import router as portfolio_router
from routes.execution_intel_routes import router as execution_intel_router
from routes.research_routes import router as research_router
from routes.crypto_routes import router as crypto_router
from routes.data_freshness_routes import router as data_freshness_router
from routes.shadow_routes import router as shadow_router
from routes.quant_routes import router as quant_router
from routes.scalp_routes import router as scalp_router
from routes.postmortem_routes import router as postmortem_router
from routes.auto_heal_routes import router as auto_heal_router
from routes.preferences_routes import router as preferences_router
from routes.insights_routes import router as insights_router
from routes.optimizer_routes import router as optimizer_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("trading-bot")

app = FastAPI(title="AI Trading Bot API", version="1.1.0")

api_router = APIRouter(prefix="/api")


@api_router.get("/")
async def root():
    return {"service": "ai-trading-bot", "status": "ok"}


@api_router.get("/health")
async def health():
    db = get_db()
    try:
        await db.command("ping")
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "degraded", "error": str(e)}


@api_router.get("/ea-script")
@api_router.get("/bridge/download-ea")
async def ea_script():
    path = Path(__file__).parent / "static" / "EmergentTradingBridge.mq5"
    if not path.exists():
        return {"error": "EA file missing"}
    # No-cache headers so MT5 / browsers always pull the latest version —
    # otherwise users reinstall and still get an old cached .mq5.
    return FileResponse(path, media_type="text/plain",
                        filename="EmergentTradingBridge.mq5",
                        headers={
                            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                            "Pragma": "no-cache",
                            "Expires": "0",
                        })


@api_router.get("/setup/installer.ps1")
async def installer_script():
    """Serve the PowerShell auto-installer for MT5 hosts.

    The installer is intentionally public (no auth) — it carries no
    secrets and only becomes useful when paired with a token via
    POST /api/setup/claim-pairing. Surfaced as a top-level URL so users
    can `irm <backend>/api/setup/installer.ps1 | iex` on their VPS.
    """
    path = Path(__file__).parent / "static" / "STOIC-Installer.ps1"
    if not path.exists():
        return {"error": "Installer missing"}
    return FileResponse(path, media_type="text/plain",
                        filename="STOIC-Installer.ps1",
                        headers={
                            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                            "Pragma": "no-cache",
                        })


# Mount routers
api_router.include_router(admin_router)
api_router.include_router(migration_router)
api_router.include_router(setup_router)
api_router.include_router(auth_router)
api_router.include_router(market_router)
api_router.include_router(bot_router)
api_router.include_router(signal_router)
api_router.include_router(account_router)
api_router.include_router(trade_router)
api_router.include_router(bridge_router)
api_router.include_router(sentiment_router)
api_router.include_router(calendar_router)
api_router.include_router(panic_router)
api_router.include_router(nl_router)
api_router.include_router(copilot_router)
api_router.include_router(bugs_router)
api_router.include_router(subscription_router)
api_router.include_router(affiliate_router)
api_router.include_router(notification_router)
api_router.include_router(telegram_router)
api_router.include_router(posture_router)
api_router.include_router(rl_router)
api_router.include_router(ml_router)
api_router.include_router(architecture_router)
api_router.include_router(analytics_router)
api_router.include_router(entitlement_router)
api_router.include_router(agent_router)
api_router.include_router(partner_router)
api_router.include_router(integrity_router)
api_router.include_router(diagnostic_router)
api_router.include_router(safety_blocks_router)
api_router.include_router(macro_router)
api_router.include_router(strategies_router)
api_router.include_router(portfolio_router)
api_router.include_router(execution_intel_router)
api_router.include_router(research_router)
api_router.include_router(crypto_router)
api_router.include_router(data_freshness_router)
api_router.include_router(shadow_router)
api_router.include_router(quant_router)
api_router.include_router(scalp_router)
api_router.include_router(postmortem_router)
api_router.include_router(auto_heal_router)
api_router.include_router(preferences_router)
api_router.include_router(insights_router)
api_router.include_router(optimizer_router)


# ---------- WebSocket ----------
@api_router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    """Authenticated WS: reads access_token cookie OR ?token=... query param."""
    token = websocket.cookies.get("access_token")
    if not token:
        token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4401)
        return
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            await websocket.close(code=4401)
            return
        user_id = payload["sub"]
        db = get_db()
        if not await db.users.find_one({"_id": ObjectId(user_id)}):
            await websocket.close(code=4401)
            return
    except Exception:
        await websocket.close(code=4401)
        return

    await ws_manager.connect(user_id, websocket)
    try:
        await websocket.send_json({"type": "connected", "payload": {"user_id": user_id}})
        while True:
            # Keep the socket alive; we ignore client messages but consume them
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(user_id, websocket)


app.include_router(api_router)


# Belt-and-suspenders: any route that forgets to use route_utils.parse_object_id
# and crashes on a malformed Mongo ObjectId now returns a clean 404 instead of
# a 500. Prevents the same class of bug (iter22 P2.2) from re-emerging when
# new routes are added.
from bson.errors import InvalidId
from fastapi import Request
from fastapi.responses import JSONResponse


@app.exception_handler(InvalidId)
async def _invalid_id_handler(_request: Request, _exc: InvalidId):
    return JSONResponse(status_code=404, content={"detail": "Resource not found"})


# CORS — credentialed cookies must NEVER be paired with a wildcard origin.
# Either: explicit allowlist with credentials, OR wildcard WITHOUT credentials.
# If CORS_ORIGINS is unset/wildcard, we strip credentials so a malicious site
# can't pull authenticated calls from a logged-in browser.
cors_origins_env = (os.environ.get("CORS_ORIGINS") or "").strip()
if cors_origins_env and cors_origins_env != "*":
    allowed = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # Safe wildcard: no credentials. Browsers won't send/receive cookies cross-origin.
    # API can still serve public/non-cookie traffic.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


_bot_runner_task = None
_warmer_task = None
_trade_manager_task = None
_auto_heal_task = None
_stuck_sync_task = None
_optimizer_task = None
_nightly_tuner_task = None
_scalp_reconcile_task = None


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


async def _scalp_reconcile_loop():
    """Round 9 cadences — one scheduler, three sweep frequencies:
      • protection safety sweep: every 10s (unprotected scalps can't wait)
      • financial pending-deal sweep: every 45s
      • full invariant + durable-state sweep: every 300s"""
    from scalp.engine import (recover_pending_deals, sweep_submission_slots,
                              verify_account_invariants,
                              verify_durable_invariants, _runners)
    from protection_guard import repair_unprotected_positions
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
            await repair_unprotected_positions(get_db())
            now_m = monotonic()
            if now_m >= next_slot:
                next_slot = now_m + SLOT
                await sweep_submission_slots(get_db())
            if now_m >= next_fin:
                next_fin = now_m + FIN
                await recover_pending_deals(get_db())
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
                # Best-effort backfill: exit ≈ entry (breakeven placeholder)
                entry = float(t.get("entry_price") or 0)
                await db.trades.update_one(
                    {"_id": t["_id"]},
                    {"$set": {
                        "exit_price": entry,
                        "pnl": float(t.get("pnl") or 0),
                        "exit_price_source": "auto_backfill_broker_confirmed_missing",
                        "exit_price_backfilled_at": datetime.now(timezone.utc).isoformat(),
                        "ghost_acknowledged": True,
                    }},
                )
                backfilled += 1
                logger.info(
                    "exit-price backfilled trade=%s ticket=%s user=%s (broker no longer holds)",
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


@app.on_event("startup")
async def on_startup():
    global _bot_runner_task, _warmer_task, _trade_manager_task, _auto_heal_task, _stuck_sync_task, _optimizer_task, _nightly_tuner_task, _scalp_reconcile_task
    try:
        await ensure_indexes()
        await seed_admin()
        from seed import dependency_health_check
        await dependency_health_check()
        logger.info("Startup: indexes ensured, admin seeded.")
        _bot_runner_task = asyncio.create_task(bot_runner.loop())
        _warmer_task = asyncio.create_task(warmer.loop())
        _trade_manager_task = asyncio.create_task(trade_manager.run_loop())
        _auto_heal_task = asyncio.create_task(_auto_heal_loop())
        _stuck_sync_task = asyncio.create_task(_stuck_open_sync_loop())
        _optimizer_task = asyncio.create_task(_optimizer_loop())
        _nightly_tuner_task = asyncio.create_task(_nightly_tuning_loop())
        _scalp_reconcile_task = asyncio.create_task(_scalp_reconcile_loop())
        _eod_flatten_task = asyncio.create_task(_eod_flatten_loop())
        logger.info("Bot runner + warmer + trade manager + auto-heal + stuck-sync + optimizer + nightly-tuner scheduled.")
    except Exception as e:
        logger.exception("Startup error: %s", e)


@app.on_event("shutdown")
async def on_shutdown():
    for task in (_bot_runner_task, _warmer_task, _trade_manager_task,
                 _auto_heal_task, _stuck_sync_task, _optimizer_task,
                 _nightly_tuner_task, _scalp_reconcile_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    await close_client()

