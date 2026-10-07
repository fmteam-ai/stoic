"""Pairing alerts — a VPS terminal that was paired by the installer but never heartbeats is the #1
demo blocker (WebRequest URL missing, EA not attached, AutoTrading off). Detect it after
PAIRING_HEARTBEAT_ALERT_SEC (default 10 min), raise an ops alert and push it to the security
Telegram chat; announce the recovery when the first heartbeat lands. Pure planning here, I/O in
`evaluate()` (called by alerting.evaluate_ops_alerts every cycle)."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("pairing_alerts")

KIND = "pairing_no_heartbeat"
DEFAULT_ALERT_SEC = 600            # 10 minutes after pairing without a heartbeat
DEFAULT_MAX_AGE_SEC = 7 * 86400    # a pairing silent for a week is abandoned, not an incident
SCAN_LIMIT = 2000                  # SA6-P3 — bounded scan per cycle (a fleet is tens of terminals, not thousands)


def _parse(v) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def alert_sec() -> int:
    return int(os.environ.get("PAIRING_HEARTBEAT_ALERT_SEC", DEFAULT_ALERT_SEC))


def max_age_sec() -> int:
    return int(os.environ.get("PAIRING_ALERT_MAX_AGE_SEC", DEFAULT_MAX_AGE_SEC))


def dedup_key(account_id) -> str:
    return f"pairing_heartbeat:{account_id}"


def silent_pairing(acc: dict, now: datetime, *, alert_after: int, ceiling: int) -> dict | None:
    """The pairing facts when `acc` was paired ≥ alert_after seconds ago and no heartbeat arrived since."""
    paired = _parse(acc.get("installer_paired_at"))
    if not paired or acc.get("status") == "deleted":
        return None
    since = (now - paired).total_seconds()
    if since < alert_after or since > ceiling:
        return None
    hb = _parse(acc.get("last_heartbeat"))
    if hb and hb >= paired:
        return None
    return {"paired_at": paired, "silent_s": int(since), "never": hb is None,
            "host": acc.get("installer_paired_hostname") or "unknown host",
            "installer_version": acc.get("installer_version")}


def alert_text(acc: dict, info: dict, url: str) -> str:
    label = acc.get("label") or str(acc.get("_id"))
    num = str(acc.get("account_number") or "")
    masked = ("…" + num[-3:]) if len(num) > 3 else num           # SA6-P3 — never the full login number in a chat
    ident = " · ".join(str(x) for x in (acc.get("broker"), masked) if x)
    mins = info["silent_s"] // 60
    return (
        "STOIC · VPS PAIRING SILENT\n"
        f"Account: {label}" + (f" ({ident})" if ident else "") + "\n"
        f"Paired: {info['host']} at {info['paired_at'].strftime('%Y-%m-%d %H:%M')} UTC"
        + (f" (installer v{info['installer_version']})" if info.get("installer_version") else "") + "\n"
        f"No EA heartbeat for {mins} min since pairing" + (" (never heartbeated)" if info["never"] else "") + ".\n"
        "Likely: WebRequest URL not allowed, EA not attached, or AutoTrading off.\n"
        f"Fix in MT5: Tools → Options → Expert Advisors → allow WebRequest for {url or '<your STOIC URL>'};\n"
        "attach EmergentTradingBridge to a chart; AutoTrading ON. Accounts → Installer Progress shows the live steps."
    )


def owner_lines(acc: dict, info: dict, url: str) -> list[str]:
    """Easy-Connect P1.4 — the OWNER's message: what happened + the exact fix, no ops jargon."""
    label = acc.get("label") or str(acc.get("_id"))
    mins = info["silent_s"] // 60
    return [
        f"Your MT5 terminal on {info['host']} has not sent data for {mins} min since it was paired for {label}.",
        "Fix (2 minutes, in MT5 on your VPS):",
        f"1. Tools → Options → Expert Advisors → tick 'Allow WebRequest for listed URL' → add {url or '<your STOIC URL>'}",
        "2. Make sure EmergentTradingBridge is on a chart and the AutoTrading button is green",
        "3. Leave the EA inputs at default — server URL, token and installation id are auto-loaded",
        "Accounts → Install Progress shows each step turn green; this alert closes itself on the first heartbeat.",
    ]


async def notify_owner(db, acc: dict, info: dict, url: str) -> None:
    """Telegram (safety event → never paywalled) + e-mail to the account owner, best effort.
    `info` is the alert meta ({host, silent_s, never_heartbeated, …})."""
    uid = str(acc.get("user_id") or "")
    if not uid:
        return
    lines = owner_lines(acc, info, url)
    try:
        from notifier import send_telegram as user_tg
        await user_tg(uid, "heartbeat_lost", "VPS not sending data", lines)
    except Exception as e:  # noqa: BLE001
        logger.warning("owner telegram failed: %s", type(e).__name__)
    try:
        from bson import ObjectId
        from email_sender import send_email
        user = await db.users.find_one({"_id": ObjectId(uid)}, {"email": 1}) if ObjectId.is_valid(uid) else None
        email = (user or {}).get("email")
        if email:
            from html import escape
            html = "<p>" + "</p><p>".join(escape(ln) for ln in lines) + "</p>"   # audit #10 — host/label are client-supplied
            await send_email(email, "STOIC — your VPS stopped sending data", html, text="\n".join(lines),
                             idempotency_key=f"pairing_silent:{acc.get('_id')}:{acc.get('installer_paired_at')}")
    except Exception as e:  # noqa: BLE001
        logger.warning("owner email failed: %s", type(e).__name__)


def recovery_text(acc: dict) -> str:
    label = acc.get("label") or str(acc.get("_id"))
    return f"STOIC · VPS PAIRING RECOVERED\nAccount: {label} — EA heartbeat received, terminal is talking again."


def heartbeat_after_pairing(acc: dict) -> bool:
    """N106-4 — a RECOVERY is a heartbeat that really arrived after the pairing — not an abandoned,
    deleted or re-paired terminal that merely stopped matching the alert condition."""
    paired, hb = _parse(acc.get("installer_paired_at")), _parse(acc.get("last_heartbeat"))
    return bool(paired and hb and hb >= paired)


def plan(accounts: list[dict], open_keys: set[str], now: datetime | None = None, *, url: str = "",
         alert_after: int | None = None, ceiling: int | None = None, is_test=None) -> dict:
    """{'active': keys that hold now, 'raise': [(key, text, meta, acc)], 'recovered': [(key, text)]}.
    `is_test(acc)` marks synthetic accounts: alerted (dedup'd in ops_alerts) but never pushed to Telegram."""
    now = now or datetime.now(timezone.utc)
    alert_after = alert_sec() if alert_after is None else alert_after
    ceiling = max_age_sec() if ceiling is None else ceiling
    is_test = is_test or (lambda a: bool(a.get("synthetic")))
    active, to_raise = set(), []
    by_key = {}
    for acc in accounts:
        key = dedup_key(acc.get("_id"))
        by_key[key] = acc
        info = silent_pairing(acc, now, alert_after=alert_after, ceiling=ceiling)
        if info:
            active.add(key)
            to_raise.append((key, alert_text(acc, info, url),
                             {"account_id": str(acc.get("_id")), "host": info["host"], "silent_s": info["silent_s"],
                              "never_heartbeated": info["never"], "webrequest_url": url}, acc))
    recovered = [(k, recovery_text(by_key[k])) for k in sorted(open_keys)
                 if k not in active and k in by_key and heartbeat_after_pairing(by_key[k]) and not is_test(by_key[k])]
    return {"active": active, "raise": to_raise, "recovered": recovered}


async def evaluate(db, now: datetime | None = None, *, raise_alert, notify=None, owner_notify=None) -> tuple[set, int]:
    """Runs inside alerting.evaluate_ops_alerts: returns (active dedup keys, alerts raised).
    New alerts and real recoveries are pushed to the security Telegram chat (best effort)."""
    now = now or datetime.now(timezone.utc)
    if notify is None:
        from security_agent.alerts import send_telegram as notify
    if owner_notify is None:
        owner_notify = notify_owner
    from install_progress import webrequest_url
    try:
        from synthetic_data import is_synthetic_account as is_test
    except Exception:  # noqa: BLE001
        def is_test(a):
            return bool(a.get("synthetic"))
    accounts = [a async for a in db.accounts.find(
        {"installer_paired_at": {"$exists": True, "$ne": None}, "status": {"$ne": "deleted"}},
        {"label": 1, "broker": 1, "account_number": 1, "installer_paired_at": 1, "installer_paired_hostname": 1,
         "installer_version": 1, "last_heartbeat": 1, "synthetic": 1, "status": 1, "user_id": 1}
    ).sort("installer_paired_at", -1).limit(SCAN_LIMIT)]                       # newest pairings first — deterministic
    open_keys = {a["dedup_key"] async for a in db.ops_alerts.find({"kind": KIND, "acked_at": None}, {"dedup_key": 1})}
    p = plan(accounts, open_keys, now, url=webrequest_url(), is_test=is_test)
    if len(accounts) >= SCAN_LIMIT:
        # N107-note1 — a truncated scan proves nothing about the accounts it did not see: keep their open
        # alerts active (never silently close + reopen them), they are re-evaluated once they fall inside.
        scanned = {dedup_key(a.get("_id")) for a in accounts}
        p["active"] |= {k for k in open_keys if k not in scanned}
    raised = 0
    for key, text, meta, acc in p["raise"]:
        synthetic = bool(is_test(acc))
        new_id = await raise_alert(db, KIND, "critical", text.splitlines()[0] + f" — {acc.get('label') or acc.get('_id')}: "
                                   f"no heartbeat {meta['silent_s'] // 60} min after pairing on {meta['host']}",
                                   dedup_key=key, meta=meta, synthetic=synthetic)
        if new_id:
            raised += 1
            if not synthetic:
                try:
                    await notify(text)
                except Exception as e:  # noqa: BLE001 — Telegram must never break the evaluator
                    logger.warning("pairing alert telegram failed: %s", type(e).__name__)
                try:   # P1.4 — the user is told what to fix, not only ops
                    await owner_notify(db, acc, meta, webrequest_url())
                except Exception as e:  # noqa: BLE001
                    logger.warning("pairing owner notify failed: %s", type(e).__name__)
    for key, text in p["recovered"]:
        try:
            await notify(text)
        except Exception as e:  # noqa: BLE001
            logger.warning("pairing recovery telegram failed: %s", type(e).__name__)
    return p["active"], raised
