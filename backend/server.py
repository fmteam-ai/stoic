from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / ".env")

import os
import logging
from fastapi import FastAPI, APIRouter
from fastapi.responses import FileResponse
from starlette.middleware.cors import CORSMiddleware

from database import close_client, get_db
from seed import seed_admin, ensure_indexes

# Routers
from routes.auth_routes import router as auth_router
from routes.market_routes import router as market_router
from routes.bot_routes import router as bot_router
from routes.signal_routes import router as signal_router
from routes.account_routes import router as account_router
from routes.trade_routes import router as trade_router
from routes.bridge_routes import router as bridge_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("trading-bot")

app = FastAPI(title="AI Trading Bot API", version="1.0.0")

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
    """Serve the MT5 Expert Advisor source code for download."""
    path = Path(__file__).parent / "static" / "EmergentTradingBridge.mq5"
    if not path.exists():
        return {"error": "EA file missing"}
    return FileResponse(
        path,
        media_type="text/plain",
        filename="EmergentTradingBridge.mq5",
    )


# Mount routers
api_router.include_router(auth_router)
api_router.include_router(market_router)
api_router.include_router(bot_router)
api_router.include_router(signal_router)
api_router.include_router(account_router)
api_router.include_router(trade_router)
api_router.include_router(bridge_router)

app.include_router(api_router)

# CORS — allow credentials with explicit origin from env, fallback to wildcard list.
cors_origins_env = os.environ.get("CORS_ORIGINS", "*")
if cors_origins_env.strip() == "*":
    # When credentials=True, browsers reject "*" — use regex catch-all instead.
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


@app.on_event("startup")
async def on_startup():
    try:
        await ensure_indexes()
        await seed_admin()
        logger.info("Startup: indexes ensured, admin seeded.")
    except Exception as e:
        logger.exception("Startup error: %s", e)


@app.on_event("shutdown")
async def on_shutdown():
    await close_client()
