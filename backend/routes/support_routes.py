"""Support ticket system (iter-135).

POST /api/support/tickets            — open a ticket (category, subject, message)
GET  /api/support/tickets            — the user's own tickets
GET  /api/support/tickets/{id}       — one ticket + thread (owner or admin)
POST /api/support/tickets/{id}/reply — reply in-thread (owner or admin)
POST /api/support/tickets/{id}/close — close (owner or admin)
GET  /api/support/admin/tickets      — admin queue (?status=)

Email notifications (fail-open): new ticket / user reply -> SUPPORT_NOTIFY_EMAIL,
admin reply -> ticket owner.
"""
import logging
import os
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from database import get_db
from security import rate_limit, client_ip

logger = logging.getLogger("support")
router = APIRouter(prefix="/support", tags=["support"])

CATEGORIES = {"billing", "technical", "account", "other"}
OPEN_STATUSES = ("open", "answered")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize(t: dict, with_thread: bool = False) -> dict:
    out = {
        "id": str(t["_id"]),
        "category": t.get("category"),
        "subject": t.get("subject"),
        "status": t.get("status"),
        "user_email": t.get("user_email"),
        "created_at": t.get("created_at"),
        "updated_at": t.get("updated_at"),
        "last_reply_by": t.get("last_reply_by"),
        "message_count": len(t.get("messages") or []),
    }
    if with_thread:
        out["messages"] = t.get("messages") or []
    return out


async def _notify(recipient: str, subject: str, body_html: str) -> None:
    try:
        import email_sender
        if email_sender.is_configured() and recipient:
            await email_sender.send_email(recipient=recipient,
                                          subject=subject, html=body_html)
    except Exception:  # noqa: BLE001
        logger.warning("support email notify failed (to=%s)", recipient)


def _email_html(title: str, lines: list[str], cta_path: str) -> str:
    body = "".join(
        f'<div style="font-size:13px;color:#A1A1AA;line-height:1.6;margin-bottom:8px">{l}</div>'
        for l in lines)
    return f"""
<div style="background:#0A0A0A;color:#FAFAFA;font-family:'Courier New',monospace;padding:32px;max-width:560px;margin:auto;border:1px solid #1F1F1F">
  <div style="color:#00FF41;font-size:20px;font-weight:bold;letter-spacing:4px;margin-bottom:20px">STOIC</div>
  <div style="font-size:15px;margin-bottom:14px">{title}</div>
  {body}
  <a href="https://stoicaibot.com{cta_path}"
     style="display:inline-block;background:#00FF41;color:#000;padding:10px 24px;font-size:13px;letter-spacing:2px;text-decoration:none;font-weight:bold;margin-top:12px">OPEN TICKET</a>
</div>"""


def _oid(ticket_id: str) -> ObjectId:
    try:
        return ObjectId(ticket_id)
    except Exception:
        raise HTTPException(status_code=404, detail="ticket not found")


async def _load_for(db, ticket_id: str, user: dict) -> dict:
    t = await db.support_tickets.find_one({"_id": _oid(ticket_id)})
    if not t:
        raise HTTPException(status_code=404, detail="ticket not found")
    if user.get("role") != "admin" and t.get("user_id") != user["id"]:
        raise HTTPException(status_code=404, detail="ticket not found")
    return t


@router.post("/tickets")
async def create_ticket(payload: dict, request: Request,
                        user=Depends(get_current_user)):
    db = get_db()
    await rate_limit(db, "support_create", user["id"], 5, 3600,
                     "Too many tickets opened. Try again later.",
                     request=request)
    category = (payload.get("category") or "").strip().lower()
    subject = (payload.get("subject") or "").strip()[:200]
    message = (payload.get("message") or "").strip()[:5000]
    if category not in CATEGORIES:
        raise HTTPException(status_code=400,
                            detail=f"category must be one of {sorted(CATEGORIES)}")
    if len(subject) < 3 or len(message) < 10:
        raise HTTPException(status_code=400,
                            detail="subject (min 3 chars) and message (min 10 chars) required")
    now = _now_iso()
    doc = {
        "user_id": user["id"],
        "user_email": user.get("email", ""),
        "category": category,
        "subject": subject,
        "status": "open",
        "created_at": now,
        "updated_at": now,
        "last_reply_by": "user",
        "messages": [{"by": "user", "author_email": user.get("email", ""),
                      "body": message, "at": now}],
    }
    r = await db.support_tickets.insert_one(doc)
    tid = str(r.inserted_id)
    await _notify(
        os.environ.get("SUPPORT_NOTIFY_EMAIL", ""),
        f"[STOIC support] New {category} ticket: {subject}",
        _email_html("New support ticket",
                    [f"From: {user.get('email','')}",
                     f"Category: {category}", f"Subject: {subject}",
                     message[:400]],
                    "/admin/support"),
    )
    doc["_id"] = r.inserted_id
    return _serialize(doc, with_thread=True)


@router.get("/tickets")
async def my_tickets(user=Depends(get_current_user)):
    db = get_db()
    cursor = db.support_tickets.find({"user_id": user["id"]}) \
        .sort("updated_at", -1).limit(100)
    return [_serialize(t) async for t in cursor]


@router.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: str, user=Depends(get_current_user)):
    db = get_db()
    return _serialize(await _load_for(db, ticket_id, user), with_thread=True)


@router.post("/tickets/{ticket_id}/reply")
async def reply_ticket(ticket_id: str, payload: dict, request: Request,
                       user=Depends(get_current_user)):
    db = get_db()
    await rate_limit(db, "support_reply", user["id"], 30, 3600,
                     "Too many replies. Try again later.", request=request)
    t = await _load_for(db, ticket_id, user)
    if t.get("status") == "closed":
        raise HTTPException(status_code=400, detail="ticket is closed — open a new one")
    body = (payload.get("message") or "").strip()[:5000]
    if len(body) < 2:
        raise HTTPException(status_code=400, detail="message required")
    is_admin = user.get("role") == "admin" and t.get("user_id") != user["id"]
    by = "admin" if is_admin else "user"
    now = _now_iso()
    await db.support_tickets.update_one(
        {"_id": t["_id"]},
        {"$push": {"messages": {"by": by, "author_email": user.get("email", ""),
                                "body": body, "at": now}},
         "$set": {"updated_at": now, "last_reply_by": by,
                  "status": "answered" if by == "admin" else "open"}},
    )
    if by == "admin":
        await _notify(
            t.get("user_email", ""),
            f"[STOIC support] Reply to: {t.get('subject','your ticket')}",
            _email_html("Support replied to your ticket",
                        [f"Subject: {t.get('subject','')}", body[:400]],
                        "/support"),
        )
    else:
        await _notify(
            os.environ.get("SUPPORT_NOTIFY_EMAIL", ""),
            f"[STOIC support] User reply: {t.get('subject','')}",
            _email_html("User replied to a ticket",
                        [f"From: {user.get('email','')}", body[:400]],
                        "/admin/support"),
        )
    return _serialize(await db.support_tickets.find_one({"_id": t["_id"]}),
                      with_thread=True)


@router.post("/tickets/{ticket_id}/close")
async def close_ticket(ticket_id: str, user=Depends(get_current_user)):
    db = get_db()
    t = await _load_for(db, ticket_id, user)
    await db.support_tickets.update_one(
        {"_id": t["_id"]},
        {"$set": {"status": "closed", "updated_at": _now_iso(),
                  "closed_by": "admin" if user.get("role") == "admin" else "user"}})
    return {"ok": True}


@router.get("/admin/tickets")
async def admin_tickets(status: str = "", user=Depends(get_current_user)):
    from auth import require_admin
    require_admin(user)
    db = get_db()
    q = {}
    if status in ("open", "answered", "closed"):
        q["status"] = status
    cursor = db.support_tickets.find(q).sort("updated_at", -1).limit(200)
    items = [_serialize(t) async for t in cursor]
    counts = {s: await db.support_tickets.count_documents({"status": s})
              for s in ("open", "answered", "closed")}
    return {"tickets": items, "counts": counts}
