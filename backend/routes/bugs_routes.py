"""Bug report routes — collect screenshot + context + user description.

Two surfaces:
  POST /api/bugs           — create a bug report (screenshot data URL + context)
  GET  /api/bugs           — admin only: list recent bug reports
  GET  /api/bugs/{id}      — admin only: full bug report incl. screenshot

Storage: MongoDB. Screenshot stored as base64 data URL (1-2MB typical; capped
at 4MB on upload). Larger payloads need object storage — future work.
"""
import logging
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db

logger = logging.getLogger("bugs")
router = APIRouter(prefix="/bugs", tags=["bugs"])

MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024  # 4 MB


@router.post("")
async def create_bug(payload: dict, user=Depends(get_current_user)):
    description = (payload.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description required")
    if len(description) > 4000:
        raise HTTPException(status_code=400, detail="description too long")

    screenshot = payload.get("screenshot")
    if screenshot and len(screenshot) > MAX_SCREENSHOT_BYTES:
        raise HTTPException(status_code=413, detail="screenshot too large (>4MB)")

    db = get_db()
    doc = {
        "user_id": user["id"],
        "user_email": user.get("email"),
        "description": description,
        "screenshot": screenshot,  # data URL string ("data:image/png;base64,...") or None
        "url": payload.get("url"),
        "user_agent": payload.get("user_agent"),
        "viewport": payload.get("viewport"),
        "console_logs": (payload.get("console_logs") or [])[:50],  # cap
        "app_snapshot": payload.get("app_snapshot"),  # last signal / bot config / panic
        "via": payload.get("via", "copilot"),  # 'copilot' | 'manual' | 'shake'
        "status": "new",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    res = await db.bug_reports.insert_one(doc)
    logger.info("Bug filed by user=%s id=%s via=%s", user["id"], res.inserted_id, doc["via"])
    return {"ok": True, "id": str(res.inserted_id),
            "received_at": doc["created_at"]}


def _is_admin(user) -> bool:
    return user.get("role") == "admin"          # fix plan S9 — role only, never an email address


@router.get("")
async def list_bugs(user=Depends(get_current_user), limit: int = 50):
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="admin only")
    db = get_db()
    cursor = db.bug_reports.find({}, {"screenshot": 0}).sort("created_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    out = []
    for d in docs:
        d["id"] = str(d.pop("_id"))
        out.append(d)
    return out


@router.get("/{bug_id}")
async def get_bug(bug_id: str, user=Depends(get_current_user)):
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="admin only")
    db = get_db()
    try:
        oid = ObjectId(bug_id)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid id")
    doc = await db.bug_reports.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    doc["id"] = str(doc.pop("_id"))
    return doc


@router.patch("/{bug_id}/status")
async def update_status(bug_id: str, payload: dict, user=Depends(get_current_user)):
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="admin only")
    new_status = payload.get("status")
    if new_status not in ("new", "triaged", "in_progress", "resolved", "wont_fix"):
        raise HTTPException(status_code=400, detail="invalid status")
    db = get_db()
    try:
        oid = ObjectId(bug_id)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid id")
    res = await db.bug_reports.update_one({"_id": oid}, {"$set": {
        "status": new_status,
        "status_updated_at": datetime.now(timezone.utc).isoformat(),
        "status_updated_by": user.get("email"),
    }})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True, "status": new_status}
