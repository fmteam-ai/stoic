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


# CORS
cors_origins_env = os.environ.get("CORS_ORIGINS", "*")
if cors_origins_env.strip() == "*":
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in cors_origins_env.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


_bot_runner_task = None
_warmer_task = None


@app.on_event("startup")
async def on_startup():
    global _bot_runner_task, _warmer_task
    try:
        await ensure_indexes()
        await seed_admin()
        logger.info("Startup: indexes ensured, admin seeded.")
        _bot_runner_task = asyncio.create_task(bot_runner.loop())
        _warmer_task = asyncio.create_task(warmer.loop())
        logger.info("Bot runner + warmer scheduled.")
    except Exception as e:
        logger.exception("Startup error: %s", e)


@app.on_event("shutdown")
async def on_shutdown():
    for task in (_bot_runner_task, _warmer_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    await close_client()

