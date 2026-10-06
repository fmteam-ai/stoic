"""Testing-period gate: new member registrations and affiliate applications are CLOSED while the
platform state `signups` says so (admin toggle) or SIGNUPS_CLOSED=true is set in the environment.
Existing users log in normally; admin and operator paths are unaffected."""
import os
from datetime import datetime, timezone

STATE_ID = "signups"
CLOSED_MESSAGE = ("New registrations are closed during the testing period. "
                  "Existing members can sign in as usual.")


def env_closed() -> bool:
    return (os.environ.get("SIGNUPS_CLOSED") or "").strip().lower() in ("1", "true", "yes", "on")


async def state(db) -> dict:
    doc = await db.platform_state.find_one({"_id": STATE_ID}, {"_id": 0}) or {}
    db_closed = bool(doc.get("closed"))
    return {"closed": db_closed or env_closed(), "db_closed": db_closed, "env_closed": env_closed(),
            "updated_at": doc.get("updated_at"), "updated_by": doc.get("updated_by"),
            "message": doc.get("message") or CLOSED_MESSAGE}


async def is_closed(db) -> bool:
    return (await state(db))["closed"]


async def set_closed(db, closed: bool, actor_email: str, message: str | None = None) -> dict:
    await db.platform_state.update_one(
        {"_id": STATE_ID},
        {"$set": {"closed": bool(closed), "updated_at": datetime.now(timezone.utc).isoformat(),
                  "updated_by": actor_email, **({"message": message.strip()} if message and message.strip() else {})}},
        upsert=True)
    return await state(db)


def closed_http_exception(kind: str):
    from fastapi import HTTPException
    return HTTPException(status_code=403, detail={"code": "signups_closed", "kind": kind, "message": CLOSED_MESSAGE})
