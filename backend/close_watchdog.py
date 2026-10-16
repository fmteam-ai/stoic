"""#43/#44 — close-command watchdog.

16 Oct: five auto_deleverage_var_cap close requests on RoboForex 600007662145 were never executed (no terminal ack),
four ended "superseded", and while `close_requested` stayed true the TP1 partial / de-risk never ran.
Every bot tick, for each `requested` close command with no broker confirmation:
  • no terminal ack after UNACKED_ALERT_SEC (60 s) → ops alert `close_command_unacked` (high, once per command) + pulse;
  • MANDATORY reasons (panic, friday_flat, eod_flatten, protection, manual, NL) are NEVER expired: the request stays
    pending and is RE-DISPATCHED as a fresh fenced FULL_CLOSE with exponential backoff (60 s → 15 min) until the
    terminal confirms or the position is gone — an offline terminal must find the close waiting when it returns;
  • DISCRETIONARY reasons (var_cap / deleverage / adaptive exits / time-stop / take-profit / scalp exits) EXPIRE
    after CLOSE_REQUEST_TTL_SEC (300 s) — but only while the EA is ONLINE (fresh heartbeat): an offline terminal
    is not evidence that the close was refused. Expiry clears close_requested + the FULL_CLOSE, so trade
    management resumes; alert `close_command_expired`.
v1.60.56 (#49–#52): alerts deduped per trade+reason with critical rungs at 15 min / 1 h; no re-send / alert inside the
EA's EOD quiet window; acked netting legs skipped; late_fill_* / intentional_close / hard_drawdown are mandatory; a
discretionary close is not expired within CLOSE_ACK_GRACE_SEC of a successful terminal ack.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from bson import ObjectId

log = logging.getLogger("close-watchdog")

UNACKED_ALERT_SEC = int(os.environ.get("CLOSE_UNACKED_ALERT_SEC", "60"))
CLOSE_REQUEST_TTL_SEC = int(os.environ.get("CLOSE_REQUEST_TTL_SEC", "300"))
EA_ONLINE_MAX_AGE_SEC = int(os.environ.get("CLOSE_WATCHDOG_EA_ONLINE_SEC", "90"))
REDISPATCH_BASE_SEC = int(os.environ.get("CLOSE_REDISPATCH_BASE_SEC", "60"))
REDISPATCH_MAX_SEC = int(os.environ.get("CLOSE_REDISPATCH_MAX_SEC", "900"))

# #44 — reasons that must never be dropped: the position MUST end up closed (or confirmed gone)
# #52 — late_fill_after_* / intentional_close / hard_drawdown deleverage are mandatory too
MANDATORY_CLOSE_REASONS = frozenset({"panic", "friday_flat", "eod_flatten", "pre_news_protect", "emergency_unprotected",
                                     "emergency_specs_stale", "manual", "manual_telegram", "nl_command", "excess_over_cap",
                                     "late_fill_after_expiry", "late_fill_after_lock", "late_fill_after_panic",
                                     "late_fill_after_cancel", "intentional_close", "unprotected_position",
                                     "hard_drawdown", "auto_deleverage_hard_drawdown"})
MANDATORY_PREFIXES = ("panic", "manual", "emergency", "protection", "friday_flat", "eod_flatten", "nl_", "late_fill_after_",
                      "intentional_close")

# #50 — when two mandatory closers meet on one trade the STRONGEST reason is kept (lower rank wins)
_REASON_RANK = (("panic", 0), ("emergency", 1), ("protection", 1), ("pre_news_protect", 1), ("unprotected_position", 1),
                ("late_fill_after_", 1), ("intentional_close", 1), ("hard_drawdown", 1), ("friday_flat", 2),
                ("eod_flatten", 3), ("manual", 4), ("nl_", 4), ("excess_over_cap", 4))

# #49 — unacked-alert escalation ladder (seconds since the FIRST unconfirmed request on the trade)
ESCALATION_STEPS = ((900, "15m", "critical"), (3600, "1h", "critical"))


def is_mandatory_close(reason: str | None) -> bool:
    r = (reason or "").lower()
    return r in MANDATORY_CLOSE_REASONS or r.startswith(MANDATORY_PREFIXES)


def reason_rank(reason: str | None) -> int:
    r = (reason or "").lower()
    for prefix, rank in _REASON_RANK:
        if r == prefix or r.startswith(prefix):
            return rank
    return 5 if is_mandatory_close(r) else 9


def strongest_reason(a: str | None, b: str | None) -> str:
    """#50 — panic > protection > friday_flat > eod_flatten > manual; ties keep the first (existing) reason."""
    if not a:
        return b or ""
    if not b:
        return a
    return a if reason_rank(a) <= reason_rank(b) else b


def ea_quiet(account: dict | None, now: datetime) -> bool:
    """#49 — the EA drops FULL_CLOSE during its EOD quiet window (23:40–00:05 broker time, ApplyFullClose); the
    server mirrors that window from the account's learned broker offset (and honours an explicit heartbeat flag)."""
    from eod_quiet import is_eod_quiet
    acc = account or {}
    if acc.get("ea_quiet_window") is True:
        return True
    return is_eod_quiet(int(acc.get("broker_utc_offset_sec") or 0), now)


def redispatch_due(count: int, since_last_sec: float) -> bool:
    """Exponential backoff: 60 s, 120 s, 240 s … capped at 15 min between re-dispatches."""
    wait = min(REDISPATCH_BASE_SEC * (2 ** max(0, int(count))), REDISPATCH_MAX_SEC)
    return since_last_sec >= wait


def _dt(v) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def ea_online(account: dict | None, now: datetime) -> bool:
    hb = _dt((account or {}).get("last_heartbeat"))
    return bool(hb) and (now - hb).total_seconds() <= EA_ONLINE_MAX_AGE_SEC


async def _pulse(db, trade: dict, reason: str, level: str = "warn") -> None:
    try:
        from bot_runner import _record_pulse
        cfg = await db.bot_configs.find_one({"user_id": trade.get("user_id"), "account_id": trade.get("account_id")}) \
            or await db.bot_configs.find_one({"user_id": trade.get("user_id")})
        if cfg:
            await _record_pulse(db, cfg, symbol=trade.get("symbol"), action="ALERT", level=level, reason=reason)
    except Exception as e:  # noqa: BLE001
        log.debug("watchdog pulse failed: %s", e)


async def _account(db, trade: dict) -> dict | None:
    try:
        return await db.accounts.find_one({"_id": ObjectId(str(trade.get("account_id")))},
                                          {"last_heartbeat": 1, "label": 1, "broker_utc_offset_sec": 1, "ea_quiet_window": 1})
    except Exception:  # noqa: BLE001
        return None


def first_unconfirmed_at(trade: dict, row: dict) -> datetime | None:
    """#49 — when the FIRST (never confirmed) request on this trade was made, across re-dispatches."""
    rd = trade.get("close_redispatch") or {}
    return _dt(rd.get("first_at")) or _dt(row.get("requested_at"))


async def _redispatch(db, trade: dict, row: dict, now: datetime) -> bool:
    """#44 — re-issue a MANDATORY close as a fresh fenced FULL_CLOSE (supersedes the silent one)."""
    from close_commands import request_close
    rd = trade.get("close_redispatch") or {}
    count = int(rd.get("count") or 0)
    last = _dt(rd.get("last_at")) or _dt(row.get("requested_at")) or now
    if not redispatch_due(count, (now - last).total_seconds()):
        return False
    reason = row.get("reason") or trade.get("close_reason") or "mandatory_close"
    first_at = (first_unconfirmed_at(trade, row) or now).isoformat()
    res = await request_close(db, {"_id": trade["_id"]}, reason=reason, actor=f"close_watchdog:redispatch:{row.get('actor')}",
                              stamp={"close_redispatch": {"count": count + 1, "last_at": now.isoformat(), "first_at": first_at,
                                                          "of": row.get("command_id")}},
                              pending_modification={"type": "FULL_CLOSE", "reason": reason, "redispatch_of": row.get("command_id")},
                              emergency=True)
    if res.get("trades_marked_for_close"):
        if count == 0:   # #49 — pulse once per trade, not once per re-dispatch
            await _pulse(db, trade, f"{reason} close re-dispatched — terminal has not confirmed the previous command")
        return True
    return False


async def _escalate(db, trade: dict, row: dict, now: datetime, since_first: float) -> bool:
    """#49 — one HIGH alert per trade+reason, then CRITICAL at 15 min and again at 1 h; each rung fires once."""
    from alerting import raise_alert
    tid = str(trade["_id"])
    reason = row.get("reason")
    done = set(trade.get("close_unacked_escalations") or [])
    fired = False
    for threshold, label, severity in ESCALATION_STEPS:
        if since_first >= threshold and label not in done:
            msg = (f"{reason} close on {trade.get('symbol')} ticket {trade.get('mt5_ticket')} STILL unconfirmed after {label} "
                   f"({int((trade.get('close_redispatch') or {}).get('count') or 0)} re-dispatches, trade {tid[-6:]})")
            await raise_alert(db, "close_command_unacked", severity, msg, dedup_key=f"close_unacked:{tid}:{reason}:{label}",
                              meta={"trade_id": tid, "account_id": trade.get("account_id"), "user_id": trade.get("user_id"),
                                    "command_id": row.get("command_id"), "reason": reason, "age_sec": int(since_first),
                                    "mandatory": True, "escalation": label})
            await db.trades.update_one({"_id": trade["_id"]}, {"$addToSet": {"close_unacked_escalations": label}})
            await _pulse(db, trade, f"{reason} close still unconfirmed after {label} (trade {tid[-6:]})", level="error")
            fired = True
    return fired


async def sweep(db, now: datetime | None = None) -> dict:
    from alerting import raise_alert
    from close_commands import ack_in_grace
    now = now or datetime.now(timezone.utc)
    out = {"checked": 0, "alerted": 0, "escalated": 0, "expired": 0, "redispatched": 0, "kept_offline": 0, "quiet": 0}
    alert_cutoff = (now - timedelta(seconds=UNACKED_ALERT_SEC)).isoformat()
    rows = await db.close_commands.find({"state": "requested", "requested_at": {"$lte": alert_cutoff}}).to_list(length=200)
    for row in rows:
        out["checked"] += 1
        try:
            trade = await db.trades.find_one({"_id": ObjectId(row["trade_id"])})
        except Exception:  # noqa: BLE001
            trade = None
        if not trade or trade.get("status") != "open" or (trade.get("close_command") or {}).get("key") != row.get("command_id"):
            continue      # closed, superseded or re-issued meanwhile — the ledger row is handled by its owner
        if trade.get("netting_close_acked"):
            continue      # #51 — volume-limited netting leg already acked: the OUT deal closes it, never re-dispatch
        age = (now - (_dt(row.get("requested_at")) or now)).total_seconds()
        tid = str(trade["_id"])
        ack = row.get("terminal_ack")
        mandatory = is_mandatory_close(row.get("reason"))
        acc = await _account(db, trade)
        if mandatory and ea_quiet(acc, now):
            # #49 — the EA defers FULL_CLOSE in its EOD quiet window: no re-send, no alert until it reopens
            if not row.get("quiet_paused_at"):
                await db.close_commands.update_one({"_id": row["_id"]}, {"$set": {"quiet_paused_at": now.isoformat()}})
            out["quiet"] += 1
            continue
        since_first = (now - (first_unconfirmed_at(trade, row) or now)).total_seconds()
        if ack is None and not row.get("unacked_alert_at"):
            # #49 — dedup per trade+reason: re-dispatched rows re-use the open alert (occurrence count) instead of a new one
            msg = (f"close command {row.get('reason')} on {trade.get('symbol')} ticket {trade.get('mt5_ticket')} has no terminal "
                   f"ack after {int(since_first)}s (trade {tid[-6:]}, key {row.get('command_id')}, "
                   f"{'MANDATORY — will re-dispatch' if mandatory else 'discretionary'})")
            new = await raise_alert(db, "close_command_unacked", "high", msg, dedup_key=f"close_unacked:{tid}:{row.get('reason')}",
                                    meta={"trade_id": tid, "account_id": trade.get("account_id"), "user_id": trade.get("user_id"),
                                          "command_id": row.get("command_id"), "reason": row.get("reason"), "age_sec": int(since_first),
                                          "mandatory": mandatory})
            await db.close_commands.update_one({"_id": row["_id"]}, {"$set": {"unacked_alert_at": now.isoformat()}})
            if new is not None:
                await _pulse(db, trade, f"close not acknowledged by the terminal after {int(age)}s — {row.get('reason')} (trade {tid[-6:]})")
                out["alerted"] += 1
        if mandatory:
            # #44 — never expire; re-dispatch with backoff until confirmed or the position is gone.
            # SEC-001 — a terminal "success" ack pauses re-dispatch only for CLOSE_ACK_GRACE_SEC; a position still open
            # after that (no broker OUT deal) means the ack was wrong or forged → resume re-dispatch.
            if await _escalate(db, trade, row, now, since_first):
                out["escalated"] += 1
            if not ack_in_grace(ack, now):
                if await _redispatch(db, trade, row, now):
                    out["redispatched"] += 1
            continue
        if age >= CLOSE_REQUEST_TTL_SEC:
            if ack_in_grace(ack, now):
                continue      # #52 — the terminal said "closing" < 120 s ago: give the OUT deal time, do not expire
            if not ea_online(acc, now):
                # #44 — an offline terminal is not a refusal: keep the request pending (it will see it on return)
                if not row.get("kept_offline_at"):
                    await db.close_commands.update_one({"_id": row["_id"]}, {"$set": {"kept_offline_at": now.isoformat()}})
                out["kept_offline"] += 1
                continue
            from close_commands import expire_close
            why = f"no broker confirmation after {int(age)}s with the terminal online" + (" (terminal ack received)" if ack else " (no terminal ack)")
            if await expire_close(db, trade, row["command_id"], reason=why):
                await raise_alert(db, "close_command_expired", "high",
                                  f"close request {row.get('reason')} on {trade.get('symbol')} ticket {trade.get('mt5_ticket')} expired: {why} — "
                                  f"position still open, trade management resumed (trade {tid[-6:]})",
                                  dedup_key=f"close_expired:{row['_id']}",
                                  meta={"trade_id": tid, "account_id": trade.get("account_id"), "user_id": trade.get("user_id"),
                                        "command_id": row.get("command_id"), "reason": row.get("reason")})
                await _pulse(db, trade, f"close request expired — management resumed ({row.get('reason')}, {why}; trade {tid[-6:]})")
                out["expired"] += 1
    return out
