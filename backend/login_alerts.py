"""New-device / new-IP login notifications (iter-155).

After a successful login, if the account has previous sessions but NONE from
this IP, record an in-app security notification and best-effort email the
user. Never blocks or fails the login path.
"""
import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger("login-alerts")


async def is_new_ip(db, user_id: str, ip: str) -> bool:
    """True when the user has logged in before but never from this IP.
    Call BEFORE the new session is created."""
    if not ip or ip == "unknown":
        return False
    total = await db.auth_sessions.count_documents({"user_id": user_id})
    if total == 0:
        return False  # first-ever login — nothing to compare against
    seen = await db.auth_sessions.count_documents(
        {"user_id": user_id, "ip": ip})
    return seen == 0


def _alert_html(name: str, ip: str, user_agent: str, when: str) -> str:
    return f"""
<div style="background:#0A0A0A;color:#FAFAFA;font-family:'Courier New',monospace;padding:32px;max-width:520px;margin:auto;border:1px solid #1F1F1F">
  <div style="color:#00FF41;font-size:18px;letter-spacing:4px;margin-bottom:16px">STOIC</div>
  <div style="font-size:15px;margin-bottom:12px">New sign-in to your account</div>
  <div style="color:#A1A1AA;font-size:13px;line-height:1.7">
    Hi {name},<br/>
    Your STOIC account was just accessed from a new location.<br/><br/>
    <b>IP:</b> {ip}<br/>
    <b>Device:</b> {user_agent[:120]}<br/>
    <b>Time (UTC):</b> {when}<br/><br/>
    If this was you, no action is needed. If you don't recognize this
    activity, change your password immediately and enable authenticator 2FA
    under Settings.
  </div>
</div>"""


async def notify_new_login(db, user: dict, ip: str, user_agent: str) -> None:
    """Fire-and-forget: in-app notification + best-effort email."""
    try:
        uid = str(user["_id"])
        now = datetime.now(timezone.utc)
        await db.notifications.insert_one({
            "user_id": uid,
            "kind": "security_new_login",
            "title": "New sign-in from an unrecognized IP",
            "message": f"New sign-in from {ip} · {user_agent[:80]}",
            "severity": "warning",
            "read": False,
            "created_at": now.isoformat(),
        })
        from email_sender import send_email
        res = await send_email(
            recipient=user["email"],
            subject="STOIC — new sign-in to your account",
            html=_alert_html(user.get("name") or user["email"].split("@")[0],
                             ip, user_agent or "unknown device",
                             now.strftime("%Y-%m-%d %H:%M")),
        )
        if not res.get("ok"):
            logger.info("login alert email skipped: %s", res.get("error"))
    except Exception as e:  # noqa: BLE001 — never break the login path
        logger.warning("login alert failed: %s", e)


def schedule_new_login_alert(db, user: dict, ip: str, user_agent: str) -> None:
    asyncio.create_task(notify_new_login(db, user, ip, user_agent))
