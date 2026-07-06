"""iter-56 · End-of-Day quiet window.

Brokers widen spreads drastically in the final minutes before the daily
close (rollover). The EA (v1.41) blocks all order operations 23:40-00:05
BROKER server time; this module mirrors the guard server-side so no NEW
signal is even generated during the window (a queued signal would otherwise
execute right after at a 20-minute-old price). Uses the per-account
`broker_utc_offset_sec` learned from live deals."""
from datetime import datetime, timedelta, timezone

QUIET_START_MIN = 23 * 60 + 40   # 23:40 broker time
QUIET_END_MIN = 5                # 00:05 broker time


def is_eod_quiet(broker_offset_sec: int = 0, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    broker_now = now + timedelta(seconds=int(broker_offset_sec or 0))
    m = broker_now.hour * 60 + broker_now.minute
    return m >= QUIET_START_MIN or m < QUIET_END_MIN


def eod_quiet_block(account: dict | None) -> str | None:
    offset = int((account or {}).get("broker_utc_offset_sec") or 0)
    if is_eod_quiet(offset):
        return ("EOD quiet window (23:40-00:05 broker time) — spreads widen "
                "drastically at the daily close; order operations paused.")
    return None
