"""#43 (b)+(c) — close-command watchdog.

16 Oct: five auto_deleverage_var_cap close requests on RoboForex 600007662145 were never executed (no terminal ack),
four ended "superseded", and while `close_requested` stayed true the TP1 partial / de-risk never ran (+2.45R → +0.02R).
Every bot tick:
  • a `requested` close command with no terminal ack after UNACKED_ALERT_SEC → ops alert `close_command_unacked`
    (high, once per command) + pulse;
  • after CLOSE_REQUEST_TTL_SEC without broker confirmation → the request EXPIRES (close_commands.expire_close):
    close_requested cleared, FULL_CLOSE modification released, pulse "close request expired — management resumed",
    ops alert `close_command_expired`. The position stays open and visible; nothing is retried blindly.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from bson import ObjectId

log = logging.getLogger("close-watchdog")

UNACKED_ALERT_SEC = int(os.environ.get("CLOSE_UNACKED_ALERT_SEC", "60"))
CLOSE_REQUEST_TTL_SEC = int(os.environ.get("CLOSE_REQUEST_TTL_SEC", "300"))


def _dt(v) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


async def _pulse(db, trade: dict, reason: str, level: str = "warn") -> None:
    try:
        from bot_runner import _record_pulse
        cfg = await db.bot_configs.find_one({"user_id": trade.get("user_id"), "account_id": trade.get("account_id")}) \
            or await db.bot_configs.find_one({"user_id": trade.get("user_id")}) or {"user_id": trade.get("user_id")}
        await _record_pulse(db, cfg, symbol=trade.get("symbol"), action="ALERT", level=level, reason=reason)
    except Exception as e:  # noqa: BLE001
        log.debug("watchdog pulse failed: %s", e)


async def sweep(db, now: datetime | None = None) -> dict:
    from alerting import raise_alert
    now = now or datetime.now(timezone.utc)
    out = {"checked": 0, "alerted": 0, "expired": 0}
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
        age = (now - (_dt(row.get("requested_at")) or now)).total_seconds()
        tid = str(trade["_id"])
        ack = row.get("terminal_ack")
        if ack is None and not row.get("unacked_alert_at"):
            msg = (f"close command {row.get('reason')} on {trade.get('symbol')} ticket {trade.get('mt5_ticket')} has no terminal "
                   f"ack after {int(age)}s (trade {tid[-6:]}, key {row.get('command_id')})")
            await raise_alert(db, "close_command_unacked", "high", msg, dedup_key=f"close_unacked:{row['_id']}",
                              meta={"trade_id": tid, "account_id": trade.get("account_id"), "user_id": trade.get("user_id"),
                                    "command_id": row.get("command_id"), "reason": row.get("reason"), "age_sec": int(age)})
            await db.close_commands.update_one({"_id": row["_id"]}, {"$set": {"unacked_alert_at": now.isoformat()}})
            await _pulse(db, trade, f"close not acknowledged by the terminal after {int(age)}s — {row.get('reason')} (trade {tid[-6:]})")
            out["alerted"] += 1
        if age >= CLOSE_REQUEST_TTL_SEC:
            from close_commands import expire_close
            why = f"no broker confirmation after {int(age)}s" + (" (terminal ack received)" if ack else " (no terminal ack)")
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
