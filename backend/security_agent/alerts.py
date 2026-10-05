"""SA3 — alert delivery for Critical/High findings (observe mode). Dedicated security Telegram
chat (SECURITY_AGENT_TELEGRAM_*), admin email via Resend; one alert per dedup_key, Critical
repeats every critical_repeat_min while open. Text never carries secrets (findings are masked)."""
import html
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

from security_agent.redact import mask

log = logging.getLogger("security_agent.alerts")
TELEGRAM_API = "https://api.telegram.org"
ALERT_SEVERITIES = ("critical", "high")


def telegram_creds(env: dict | None = None) -> tuple[str, str] | None:
    env = os.environ if env is None else env
    tok, chat = (env.get("SECURITY_AGENT_TELEGRAM_BOT_TOKEN") or "").strip(), (env.get("SECURITY_AGENT_TELEGRAM_CHAT_ID") or "").strip()
    return (tok, chat) if tok and chat else None


def finding_url(finding_id) -> str:
    base = (os.environ.get("PUBLIC_BASE_URL") or os.environ.get("FRONTEND_URL")
            or (os.environ.get("CORS_ORIGINS") or "").split(",")[0].strip() or "https://stoicaibot.com").rstrip("/")
    return f"{base}/admin/ops?finding={finding_id}"


def alert_text(fd: dict, *, repeat: bool = False) -> str:
    """Plain text (also used for Telegram HTML after escaping). Masked once more for safety."""
    head = "REPEAT · " if repeat else ""
    lines = [f"[STOIC SECURITY] {head}{fd['severity'].upper()} · {fd.get('check_id')} · {fd.get('title')}",
             f"What happened: {fd.get('what_happened')}",
             f"Why it matters: {fd.get('why_it_matters')}",
             f"Action taken: {fd.get('action_taken') or 'none — alert only'}",
             f"Status: {fd.get('status')} · seen {fd.get('occurrences', 1)}× since {str(fd.get('first_seen', ''))[:16]} UTC",
             "Solution: " + " ".join(f"{i}) {s}" for i, s in enumerate(fd.get("solution") or [], 1)),
             f"Verify: {fd.get('verify')}",
             f"Finding: {finding_url(fd.get('_id') or fd.get('id'))}"]
    return mask("\n".join(lines))


async def send_telegram(text: str, *, creds: tuple[str, str] | None = None) -> bool:
    creds = creds or telegram_creds()
    if not creds:
        log.info("security telegram not configured — alert logged only")
        return False
    tok, chat = creds
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post(f"{TELEGRAM_API}/bot{tok}/sendMessage",
                             json={"chat_id": chat, "text": f"<pre>{html.escape(text[:3800])}</pre>", "parse_mode": "HTML", "disable_web_page_preview": True})
        if r.status_code != 200:
            log.warning("security telegram send failed status=%s", r.status_code)
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        log.warning("security telegram send exception: %s", type(e).__name__)
        return False


async def send_email(cfg: dict, subject: str, text: str) -> int:
    from email_sender import is_configured, send_email as _send
    if not is_configured():
        log.info("security email not configured — alert logged only")
        return 0
    body = f"<pre style='font-family:monospace;white-space:pre-wrap'>{html.escape(text)}</pre>"
    sent = 0
    for rcpt in cfg.get("alert_emails") or []:
        try:
            res = await _send(rcpt, mask(subject)[:200], body)
            sent += int(bool(res.get("ok")))
        except Exception as e:  # noqa: BLE001
            log.warning("security email to %s failed: %s", rcpt, type(e).__name__)
    return sent


def _claim_filter(cfg: dict) -> dict:
    """Open Critical/High findings that are due an alert: never alerted, or Critical older than the repeat interval
    (acknowledged findings stop repeating)."""
    repeat_cut = (datetime.now(timezone.utc) - timedelta(minutes=int(cfg.get("critical_repeat_min") or 30))).isoformat()
    retry_cut = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    return {"severity": {"$in": list(ALERT_SEVERITIES)},
            "$and": [{"$or": [{"last_alert_failed": {"$ne": True}}, {"last_alert_attempt_at": {"$lt": retry_cut}}]}],
            "$or": [
        {"status": {"$in": ["open", "contained", "acknowledged"]}, "alert_count": {"$in": [None, 0]}},
        {"status": {"$in": ["open", "contained"]}, "severity": "critical", "alert_count": {"$gte": 1}, "last_alert_at": {"$lt": repeat_cut}}]}


async def deliver(db, cfg: dict, fd: dict, *, repeat: bool, send_tg=None, send_mail=None) -> dict:
    send_tg, send_mail = send_tg or send_telegram, send_mail or send_email
    text = alert_text(fd, repeat=repeat)
    tg = await send_tg(text)
    mail = await send_mail(cfg, f"[STOIC SECURITY] {fd['severity'].upper()} {fd.get('check_id')}: {fd.get('title')}", text)
    now = datetime.now(timezone.utc).isoformat()
    if tg or mail:
        await db.security_findings.update_one({"_id": fd["_id"]}, {"$set": {"last_alert_at": now, "last_alert_attempt_at": now, "last_alert_failed": False,
                                                                        "last_alert_channels": {"telegram": tg, "email": mail}}, "$inc": {"alert_count": 1}})
    else:
        # S11 — nothing was delivered: not "sent"; retried on a later tick (5-min backoff)
        await db.security_findings.update_one({"_id": fd["_id"]}, {"$set": {"last_alert_attempt_at": now, "last_alert_failed": True},
                                                                  "$inc": {"alert_failures": 1}})
    await db.security_actions.insert_one({"kind": "alert", "finding_id": str(fd["_id"]), "dedup_key": fd["dedup_key"], "severity": fd["severity"],
                                          "repeat": repeat, "channels": {"telegram": tg, "email": mail}, "at": now, "actor": "security_agent"})
    return {"telegram": tg, "email": mail}


async def sweep(db, cfg: dict, *, send_tg=None, send_mail=None) -> int:
    """Called every tick. Atomic per-finding claim (alert_count bump happens in deliver; the filter re-check
    guards against double sends inside one process)."""
    n = 0
    rows = await db.security_findings.find(_claim_filter(cfg)).limit(50).to_list(length=50)
    for fd in rows:
        repeat = int(fd.get("alert_count") or 0) > 0
        try:
            await deliver(db, cfg, fd, repeat=repeat, send_tg=send_tg, send_mail=send_mail)
            n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("alert delivery failed for %s: %s", fd.get("dedup_key"), type(e).__name__)
    return n
