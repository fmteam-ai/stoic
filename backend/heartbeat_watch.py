"""iter-173 — heartbeat & verification watch.

Alerts the account owner (Telegram + email + in-app alert) the moment a
live account's EA stops heartbeating or loses identity verification.
Transition-based: state is persisted per account in `heartbeat_watch`,
so each outage notifies exactly once and re-arms on recovery.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger("heartbeat_watch")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _owner_email(db, user_id: str) -> str | None:
    u = await db.users.find_one({"id": user_id})
    if not u:
        try:
            from bson import ObjectId
            u = await db.users.find_one({"_id": ObjectId(user_id)})
        except Exception:  # noqa: BLE001
            u = None
    return (u or {}).get("email")


async def _notify(db, user_id: str, event_type: str, title: str,
                  lines: list, severity: str = "critical",
                  dedup: str | None = None) -> None:
    from alerting import raise_alert
    from notifier import send_telegram
    try:
        await send_telegram(user_id, event_type, title, lines)
    except Exception:  # noqa: BLE001
        logger.exception("heartbeat_watch: telegram notify failed")
    try:
        await raise_alert(db, event_type, severity,
                          f"{title} — " + " · ".join(lines),
                          dedup_key=dedup or f"{event_type}:{user_id}")
    except Exception:  # noqa: BLE001
        logger.exception("heartbeat_watch: raise_alert failed")
    try:
        from email_sender import is_configured, send_email
        if is_configured() and severity == "critical":
            email = await _owner_email(db, user_id)
            if email:
                rows = "".join(f"<p style='margin:4px 0'>{ln}</p>"
                               for ln in lines)
                await send_email(
                    to=email, subject=f"STOIC — {title}",
                    html=(f"<h3 style='margin:0 0 8px'>{title}</h3>{rows}"
                          "<p style='color:#888;font-size:12px'>Sent by the "
                          "STOIC heartbeat watch.</p>"))
    except Exception:  # noqa: BLE001
        logger.exception("heartbeat_watch: email notify failed")


async def check_once(db) -> dict:
    """One sweep over live accounts; fires on state TRANSITIONS only."""
    from state_contract import effective_connection_state
    checked, fired = 0, 0
    async for acc in db.accounts.find({"mode": {"$ne": "paper"},
                                       "last_heartbeat": {"$exists": True}}):
        checked += 1
        aid = str(acc["_id"])
        uid = acc.get("user_id")
        label = acc.get("label") or aid[-6:]
        conn = effective_connection_state(acc)
        connected = bool(conn["connected"])
        verified = bool((acc.get("ea_identity") or {}).get("authoritative"))
        st = await db.heartbeat_watch.find_one({"_id": aid}) or {}
        prev_conn = st.get("connected")
        prev_ver = st.get("verified")

        if prev_conn is True and not connected:
            fired += 1
            age = conn.get("heartbeat_age_seconds")
            await _notify(
                db, uid, "heartbeat_lost",
                f"EA heartbeat lost — {label}",
                [f"Last heartbeat {age:.0f}s ago" if age is not None
                 else "No heartbeat received",
                 "Trading is blocked until the terminal reconnects.",
                 "Check the MT5 terminal/VPS is running with the EA "
                 "attached and AutoTrading ON."],
                dedup=f"heartbeat_lost:{aid}")
        elif prev_conn is False and connected:
            await _notify(
                db, uid, "heartbeat_lost",
                f"EA heartbeat restored — {label}",
                ["The terminal is heartbeating again — trading readiness "
                 "recovers automatically."],
                severity="info", dedup=f"heartbeat_restored:{aid}")

        if prev_ver is True and not verified and connected:
            fired += 1
            reason = ((acc.get("ea_identity") or {}).get("reason")
                      or "unknown")
            await _notify(
                db, uid, "identity_lost",
                f"Terminal verification lost — {label}",
                [f"Reason: {reason}",
                 "Live execution is blocked until the terminal "
                 "re-verifies (one-click trust or re-pair)."],
                dedup=f"identity_lost:{aid}")

        await db.heartbeat_watch.update_one(
            {"_id": aid},
            {"$set": {"connected": connected, "verified": verified,
                      "user_id": uid, "label": label,
                      "checked_at": _now()}},
            upsert=True)
    return {"checked": checked, "alerts_fired": fired}
