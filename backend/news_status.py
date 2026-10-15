"""v1.60.39 — news source status shared across processes.

Calendar / RSS+AI fetches happen inside the bot_runner worker; Admin → Bot Health is served by the API process,
so in-memory status would always read "idle" in production. Every fetch persists a small status section to
platform_state._id="news_sources" (best-effort, never raises) and the ops route reads it back."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("news-status")

DOC_ID = "news_sources"


def newsapi_configured() -> bool:
    return bool(os.environ.get("NEWSAPI_KEY", ""))


async def record_source_status(section: str, payload: dict) -> None:
    try:
        from database import get_db
        await get_db().platform_state.update_one(
            {"_id": DOC_ID},
            {"$set": {section: {**payload, "recorded_at": datetime.now(timezone.utc).isoformat()}}},
            upsert=True)
    except Exception as e:  # noqa: BLE001 — status is diagnostics, never blocks a fetch
        logger.debug("news status persist skipped (%s): %s", section, e)


async def load_source_status() -> dict:
    try:
        from database import get_db
        doc = await get_db().platform_state.find_one({"_id": DOC_ID}) or {}
        doc.pop("_id", None)
        return doc
    except Exception as e:  # noqa: BLE001
        logger.debug("news status load failed: %s", e)
        return {}
