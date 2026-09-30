"""iter-53 · End-of-Day flatten — no intraday bot entry is held overnight.

Every strategy engine (MTF cascades, HF scalps, breakout, range fade) is
intraday: holding through the broker's daily rollover risks swap charges,
gap risk and the EOD spread blowout. In the flatten window (default
23:15-23:40 BROKER server time, per-account `broker_utc_offset_sec`):
  • every OPEN trade with origin=auto gets a FULL_CLOSE queued for the EA
  • NEW auto entries are vetoed (bot_runner + scalp fast path)
Manual trades are NEVER touched (strict auto/manual separation). The window
deliberately ends where the EA's v1.41 EOD quiet window begins (23:40) —
the EA defers all order operations during quiet, so closes must be queued
and executed before it.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from close_commands import request_close
from eod_quiet import QUIET_START_MIN

logger = logging.getLogger(__name__)

FLATTEN_LEAD_MIN = int(os.environ.get("EOD_FLATTEN_LEAD_MIN", "25"))
FLATTEN_START_MIN = QUIET_START_MIN - FLATTEN_LEAD_MIN   # 23:15 broker time
FLATTEN_END_MIN = QUIET_START_MIN                        # 23:40 broker time


def _enabled() -> bool:
    return os.environ.get("EOD_FLATTEN_ENABLED", "true").lower() == "true"


def in_flatten_window(broker_offset_sec: int = 0,
                      now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    broker_now = now + timedelta(seconds=int(broker_offset_sec or 0))
    m = broker_now.hour * 60 + broker_now.minute
    return FLATTEN_START_MIN <= m < FLATTEN_END_MIN


def eod_flatten_block(account: dict | None,
                      now: datetime | None = None) -> str | None:
    """Veto NEW auto entries during the flatten window — otherwise the bot
    would re-enter right after its positions were flattened."""
    if not _enabled():
        return None
    offset = int((account or {}).get("broker_utc_offset_sec") or 0)
    if in_flatten_window(offset, now):
        return ("EOD flatten window — intraday positions are being closed "
                "before the daily rollover; no new entries until after the "
                "broker's daily close.")
    return None


async def sweep_eod_flatten(db, now: datetime | None = None) -> dict:
    """Queue a FULL_CLOSE on every open auto trade of every account whose
    broker clock is inside the flatten window. Idempotent: trades with any
    pending_modification in flight are left alone (re-checked next sweep)."""
    if not _enabled():
        return {"enabled": False, "queued": 0, "accounts": 0}
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    queued = 0
    accounts_hit = 0
    async for acc in db.accounts.find(
            {}, {"broker_utc_offset_sec": 1, "user_id": 1, "name": 1}):
        offset = int(acc.get("broker_utc_offset_sec") or 0)
        if not in_flatten_window(offset, now):
            continue
        account_id = str(acc["_id"])
        acct_queued = 0
        async for tr in db.trades.find(
                {"account_id": account_id, "status": "open",
                 "origin": "auto"}):
            if tr.get("pending_modification"):
                continue                    # close/modify already in flight
            await request_close(db, {"_id": tr["_id"]}, reason="eod_flatten", actor="eod_flatten",
                                stamp={"eod_flatten_queued_at": now_iso,
                                       "pending_modification": {"type": "FULL_CLOSE", "reason": "eod_flatten",
                                                                "requested_at": now_iso}})
            acct_queued += 1
        if acct_queued:
            accounts_hit += 1
            queued += acct_queued
            logger.info("EOD flatten: queued %d close(s) on account %s "
                        "before broker rollover", acct_queued, account_id)
            if acc.get("user_id"):
                try:
                    await db.notifications.insert_one({
                        "user_id": acc["user_id"], "type": "eod_flatten",
                        "title": "End-of-day flatten",
                        "message": (
                            f"{acct_queued} intraday bot position(s) on "
                            f"{acc.get('name') or 'your account'} queued to "
                            f"close before the daily rollover — nothing is "
                            f"held overnight."),
                        "created_at": now_iso, "read": False})
                except Exception:  # noqa: BLE001 — notification best-effort
                    pass
    return {"enabled": True, "queued": queued, "accounts": accounts_hit}
