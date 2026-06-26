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
from routes.analytics_routes import router as analytics_router
from routes.entitlement_routes import router as entitlement_router
from routes.agent_routes import router as agent_router
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
from routes.postmortem_routes import router as postmortem_router
from routes.auto_heal_routes import router as auto_heal_router
from routes.preferences_routes import router as preferences_router

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
    return FileResponse(path, media_type="text/plain",
                        filename="EmergentTradingBridge.mq5")


# Mount routers
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
api_router.include_router(analytics_router)
api_router.include_router(entitlement_router)
api_router.include_router(agent_router)
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
api_router.include_router(postmortem_router)
api_router.include_router(auto_heal_router)
api_router.include_router(preferences_router)


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


@app.on_event("startup")
async def on_startup():
    global _bot_runner_task, _warmer_task, _trade_manager_task, _auto_heal_task
    try:
        await ensure_indexes()
        await seed_admin()
        logger.info("Startup: indexes ensured, admin seeded.")
        _bot_runner_task = asyncio.create_task(bot_runner.loop())
        _warmer_task = asyncio.create_task(warmer.loop())
        _trade_manager_task = asyncio.create_task(trade_manager.run_loop())
        _auto_heal_task = asyncio.create_task(_auto_heal_loop())
        logger.info("Bot runner + warmer + trade manager + auto-heal scheduled.")
    except Exception as e:
        logger.exception("Startup error: %s", e)


@app.on_event("shutdown")
async def on_shutdown():
    for task in (_bot_runner_task, _warmer_task, _trade_manager_task, _auto_heal_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    await close_client()

