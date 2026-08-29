"""Guard email alerts (iter-152) — email admins the moment a guard blocks
trading or telemetry goes stale. Cooldown-deduped per dedup key; delivery
via Resend (email_sender). Never blocks the trade path: callers queue a
fire-and-forget task."""
import asyncio
import html
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("guard_alerts")

COOLDOWN_S = int(os.environ.get("GUARD_ALERT_COOLDOWN_S", "1800"))


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


async def _recipients(db) -> list:
    override = [e.strip() for e in
                os.environ.get("ALERT_EMAILS", "").split(",") if e.strip()]
    if override:
        return override
    return [u["email"] async for u in
            db.users.find({"role": "admin"}, {"email": 1}).limit(10)
            if u.get("email")]


async def _claim_cooldown(db, dedup_key: str, cooldown_s: int) -> bool:
    """Atomic per-key cooldown claim — True exactly once per window."""
    from pymongo.errors import DuplicateKeyError
    now = _now_dt()
    cutoff = (now - timedelta(seconds=cooldown_s)).isoformat()
    try:
        r = await db.guard_alert_emails.update_one(
            {"_id": dedup_key, "last_sent_at": {"$lt": cutoff}},
            {"$set": {"last_sent_at": now.isoformat()}, "$inc": {"sends": 1}},
            upsert=True)
    except DuplicateKeyError:
        return False
    return bool(r.modified_count or r.upserted_id)


async def email_admins(db, subject: str, html_body: str, dedup_key: str,
                       cooldown_s: int = COOLDOWN_S) -> dict:
    from email_sender import is_configured, send_email
    if not await _claim_cooldown(db, dedup_key, cooldown_s):
        return {"ok": False, "skipped": "cooldown"}
    await db.guard_alert_emails.update_one(
        {"_id": dedup_key}, {"$set": {"subject": subject}})
    if not is_configured():
        logger.warning("guard alert (RESEND_API_KEY not set) [%s] %s",
                       dedup_key, subject)
        await db.guard_alert_emails.update_one(
            {"_id": dedup_key},
            {"$set": {"last_error": "email_not_configured"}})
        return {"ok": False, "skipped": "not_configured"}
    sent = 0
    for rcpt in await _recipients(db):
        res = await send_email(rcpt, subject, html_body)
        if res.get("ok"):
            sent += 1
        else:
            logger.warning("guard alert email to %s failed: %s",
                           rcpt, res.get("error"))
    await db.guard_alert_emails.update_one(
        {"_id": dedup_key}, {"$set": {"last_delivered": sent}})
    return {"ok": sent > 0, "sent": sent}


def _wrap(title: str, rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;color:#888'>{html.escape(k)}"
        f"</td><td style='padding:4px 0'><b>{html.escape(str(v))}</b></td></tr>"
        for k, v in rows)
    return (f"<div style='font-family:monospace'>"
            f"<h2 style='color:#c00'>{html.escape(title)}</h2>"
            f"<table>{body}</table>"
            f"<p style='color:#888;font-size:12px'>STOIC guard alert — "
            f"repeated identical alerts are muted for "
            f"{COOLDOWN_S // 60} minutes.</p></div>")


# ─────────────────────── guard BLOCK alerts ───────────────────────────────

def queue_block_alert(db, snap: dict) -> None:
    """Fire-and-forget from the execution gate — must never raise."""
    try:
        asyncio.get_running_loop().create_task(_send_block_alert(db, snap))
    except RuntimeError:
        pass


async def _send_block_alert(db, snap: dict) -> None:
    try:
        reason = str(snap.get("reason") or "blocked")
        sig = snap.get("signal") or {}
        key = f"guard_block:{snap.get('program_id')}:{reason}"
        subject = (f"[STOIC GUARD] BLOCKED {sig.get('symbol') or '?'} "
                   f"{sig.get('side') or '?'} — {reason}")
        rows = [("Reason", reason),
                ("Symbol / Side",
                 f"{sig.get('symbol')} {sig.get('side')}"),
                ("Program", snap.get("program_id")),
                ("Account", snap.get("account_id")),
                ("Mode", snap.get("mode")),
                ("Snapshot", snap.get("snapshot_id")),
                ("At", snap.get("at")),
                ("Commit", (snap.get("provenance") or {}).get("git_commit"))]
        await email_admins(db, subject,
                           _wrap("Guard blocked an execution", rows), key)
    except Exception as e:  # noqa: BLE001 — alerting must never crash
        logger.warning("guard block alert failed: %s", e)


# ─────────────── critical ops alerts (stale telemetry etc.) ───────────────

def queue_ops_alert_email(db, kind: str, severity: str, message: str,
                          dedup_key: str) -> None:
    try:
        asyncio.get_running_loop().create_task(
            _send_ops_alert(db, kind, severity, message, dedup_key))
    except RuntimeError:
        pass


async def _send_ops_alert(db, kind: str, severity: str, message: str,
                          dedup_key: str) -> None:
    try:
        subject = f"[STOIC {severity.upper()}] {kind}"
        rows = [("Kind", kind), ("Severity", severity),
                ("Detail", message), ("At", _now_dt().isoformat())]
        await email_admins(db, subject,
                           _wrap("Operational alert", rows),
                           f"ops_email:{dedup_key}")
    except Exception as e:  # noqa: BLE001
        logger.warning("ops alert email failed: %s", e)
