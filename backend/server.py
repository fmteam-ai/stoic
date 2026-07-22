from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / ".env")

import os
import asyncio
import logging
import uuid
from fastapi import FastAPI, APIRouter, Request, WebSocket, WebSocketDisconnect
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
from routes.trace_routes import router as trace_router
from routes.execution_intel_routes import router as execution_intel_router
from routes.research_routes import router as research_router
from routes.crypto_routes import router as crypto_router
from routes.data_freshness_routes import router as data_freshness_router
from routes.shadow_routes import router as shadow_router
from routes.quant_routes import router as quant_router
from routes.scalp_routes import router as scalp_router
from routes.enterprise_routes import mgmt_router as api_keys_router, public_router as enterprise_v1_router
from routes.postmortem_routes import router as postmortem_router
from routes.auto_heal_routes import router as auto_heal_router
from routes.preferences_routes import router as preferences_router
from routes.insights_routes import router as insights_router
from routes.optimizer_routes import router as optimizer_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("trading-bot")

app = FastAPI(title="AI Trading Bot API", version="1.1.0")


@app.middleware("http")
async def csrf_middleware(request, call_next):
    """CSRF double-submit enforcement for cookie-authenticated mutations
    (SameSite=None cookies make cross-site sends possible; CORS alone is
    not a CSRF defense). Bearer/bridge/webhook callers are unaffected."""
    from fastapi.responses import JSONResponse
    from security import csrf_check
    err = csrf_check(request)
    if err is not None:
        return JSONResponse(
            status_code=403,
            content={"detail": {"code": "csrf_failed", "reason": err,
                                "message": "Request blocked by CSRF "
                                           "protection. Refresh and retry."}})
    return await call_next(request)


api_router = APIRouter(prefix="/api")


@api_router.get("/")
async def root():
    return {"service": "ai-trading-bot", "status": "ok"}


_startup_error: str | None = None


@api_router.get("/health")
async def health():
    db = get_db()
    try:
        await db.command("ping")
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        # Never leak connection topology / driver internals publicly.
        cid = uuid.uuid4().hex[:12]
        logger.error("health degraded [cid=%s]: %s", cid, e)
        return {"status": "degraded", "db": "unavailable", "cid": cid}


@api_router.get("/health/live")
async def health_live():
    return {"status": "ok"}


@api_router.get("/health/ready")
async def health_ready():
    """Readiness: DB ping + critical unique indexes + submission-slot
    integrity. Degraded readiness returns 503 so orchestrators stop
    routing traffic without leaking internals."""
    from fastapi.responses import JSONResponse
    checks = {}
    ok = True
    # audit r3 P0 · critical startup failures (indexes, dependency health)
    # fail readiness so orchestrators never route to a degraded replica.
    if _startup_error is not None:
        checks["startup"] = f"failed:{_startup_error}"
        ok = False
    db = get_db()
    try:
        await db.command("ping")
        checks["db"] = "ok"
    except Exception as e:
        cid = uuid.uuid4().hex[:12]
        logger.error("readiness db fail [cid=%s]: %s", cid, e)
        checks["db"] = "unavailable"
        ok = False
    if checks["db"] == "ok":
        try:
            from seed import dependency_health_check
            dep = await dependency_health_check()
            dep_ok = bool(dep.get("deps_ok") and dep.get("db_ok")
                          and dep.get("indexes_ok"))
            checks["critical_indexes"] = "ok" if dep_ok else "missing"
            ok = ok and dep_ok
        except Exception:
            checks["critical_indexes"] = "unknown"
        from scalp.engine import capacity_integrity_reason
        cap = capacity_integrity_reason()
        checks["submission_capacity"] = "ok" if cap is None else "violated"
        ok = ok and cap is None
    body = {"status": "ok" if ok else "degraded", "checks": checks}
    return body if ok else JSONResponse(status_code=503, content=body)


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
api_router.include_router(trace_router)
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
api_router.include_router(api_keys_router)
api_router.include_router(enterprise_v1_router)


# ---------- WebSocket ----------
@api_router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    """Authenticated WS via the access_token cookie. Query-string tokens
    leak through proxy/LB logs and browser history — allowed only when
    WS_ALLOW_QUERY_TOKEN=true is set explicitly (non-production use)."""
    token = websocket.cookies.get("access_token")
    if not token and (os.environ.get("WS_ALLOW_QUERY_TOKEN", "false")
                      .lower() == "true"):
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


from background_loops import (_analytics_loop, _auto_heal_loop,
                              _eod_flatten_loop, _model_maintenance_loop,
                              _nightly_tuning_loop, _optimizer_loop,
                              _protection_guard_loop, _scalp_reconcile_loop,
                              _stuck_open_sync_loop)
_protection_task = None
_analytics_task = None
_model_maint_task = None


@app.on_event("startup")
async def on_startup():
    global _bot_runner_task, _warmer_task, _trade_manager_task, _auto_heal_task, _stuck_sync_task, _optimizer_task, _nightly_tuner_task, _scalp_reconcile_task
    # review item 5 — production hard-fails without an explicit trusted
    # Origin allowlist: CSRF_ENFORCE_ORIGIN=true + concrete CORS_ORIGINS.
    if os.environ.get("APP_ENV", "").lower() == "production":
        from security import _allowed_origins
        if (os.environ.get("CSRF_ENFORCE_ORIGIN", "false").lower() != "true"
                or not _allowed_origins()):
            raise RuntimeError(
                "APP_ENV=production requires CSRF_ENFORCE_ORIGIN=true and an "
                "explicit CORS_ORIGINS allowlist (not empty, not '*').")
    try:
        await ensure_indexes()
        await seed_admin()
        from seed import dependency_health_check
        await dependency_health_check()
        logger.info("Startup: indexes ensured, admin seeded.")
        # audit P0 · worker mode must be an EXPLICIT choice. Unset in a
        # multi-replica production API would silently double-run trading
        # loops, so the default is now OFF with a critical warning.
        worker_mode = os.environ.get("BACKGROUND_WORKERS_IN_PROCESS")
        if worker_mode is None:
            logger.critical(
                "BACKGROUND_WORKERS_IN_PROCESS is NOT SET — embedded "
                "background loops are DISABLED (fail-safe default). Set it "
                "to 'true' for single-process mode or run the dedicated "
                "workers (python -m workers.*) with it set to 'false'.")
            return
        if worker_mode.lower() != "true":
            logger.info("Background loops NOT started in-process "
                        "(external workers mode).")
            return
        _bot_runner_task = asyncio.create_task(bot_runner.loop())
        _warmer_task = asyncio.create_task(warmer.loop())
        _trade_manager_task = asyncio.create_task(trade_manager.run_loop())
        _auto_heal_task = asyncio.create_task(_auto_heal_loop())
        _stuck_sync_task = asyncio.create_task(_stuck_open_sync_loop())
        _optimizer_task = asyncio.create_task(_optimizer_loop())
        _nightly_tuner_task = asyncio.create_task(_nightly_tuning_loop())
        _scalp_reconcile_task = asyncio.create_task(_scalp_reconcile_loop())
        _eod_flatten_task = asyncio.create_task(_eod_flatten_loop())
        # Phase F — separated services (in-process mode runs them all)
        global _protection_task, _analytics_task, _model_maint_task
        _protection_task = asyncio.create_task(_protection_guard_loop())
        _analytics_task = asyncio.create_task(_analytics_loop())
        _model_maint_task = asyncio.create_task(_model_maintenance_loop())
        logger.info("Bot runner + warmer + trade manager + auto-heal + stuck-sync + optimizer + nightly-tuner scheduled.")
    except Exception as e:
        logger.exception("Startup error: %s", e)
        # audit r3 P0 · a failed startup must not serve silently: readiness
        # reports it (503) and production terminates outright.
        global _startup_error
        _startup_error = f"{type(e).__name__}"
        if os.environ.get("APP_ENV", "").lower() == "production":
            raise


@app.on_event("shutdown")
async def on_shutdown():
    for task in (_bot_runner_task, _warmer_task, _trade_manager_task,
                 _auto_heal_task, _stuck_sync_task, _optimizer_task,
                 _nightly_tuner_task, _scalp_reconcile_task,
                 _protection_task, _analytics_task, _model_maint_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    await close_client()

