"""v1.60.61 #81 — XAUUSD H4 history for gold_trend_pulse.

EA 1.65 streams M15 + M1 only, so the engine resampled 800 M15 bars into ≈ 50 H4 bars and the daily bias stayed
"NONE — H4 warm-up 50/200" forever (H4_MIN_BARS is NOT relaxed). The server now asks a capable terminal
(EA ≥ 1.66, capability `h4_history_v1`) for ≥ 250 completed H4 bars via the poll-trades response
(`candles_request` → EA `CopyRates(PERIOD_H4)` → POST /api/bridge/candles timeframe H4): once at the first poll
after start-up when the stored history is short, then daily. The stored EA bars are merged with the M15 resample
(same broker-day grid) so the newest H4 bars never go stale between backfills. 1.65 terminals ignore the key."""
from __future__ import annotations

from datetime import datetime, timezone

from bars_util import resample

SYMBOL = "XAUUSD"
TIMEFRAME = "H4"
H4_SEC = 14400
TARGET_BARS = 250          # completed bars the backfill must reach (> H4_MIN_BARS = 200 so the EMA50 is warm with margin)
REQUEST_BARS = 300         # bars asked from the terminal per request
REDISPATCH_SEC = 600       # a dropped response is re-asked after 10 min
REFRESH_SEC = 86400        # daily refresh once the history is complete
DOC_MAX_AGE_SEC = 3 * 86400    # an EA H4 doc older than this is not merged (would leave a hole before the M15 window)
CAPABILITY = "h4_history_v1"


def completed(bars: list, now_ts: float) -> list:
    return [b for b in bars if int(b["t"]) + H4_SEC <= now_ts]


def resample_anchored(m15: list, offset_sec: int) -> list:
    """M15 (UTC) → H4 bars on the BROKER day grid (MT5 H4 bars open at broker 00/04/08… = UTC − offset)."""
    if not offset_sec:
        return resample(m15, H4_SEC)
    shifted = [dict(b, t=int(b["t"]) + offset_sec) for b in m15]
    return [dict(b, t=int(b["t"]) - offset_sec) for b in resample(shifted, H4_SEC)]


def merge_h4(stored: list, m15: list, offset_sec: int, now_ts: float) -> tuple[list, str]:
    """EA H4 bars (completed, broker grid) + the M15 resample on the same grid for the newest bars.
    EA bars win on overlap. Returns (completed bars ascending, source)."""
    merged = {int(b["t"]): b for b in resample_anchored(m15, offset_sec)}
    merged.update({int(b["t"]): b for b in stored})
    bars = completed([merged[t] for t in sorted(merged)], now_ts)
    return bars, ("ea_h4+m15" if stored else "m15_resample")


def is_due(doc: dict | None, account: dict, now: datetime) -> tuple[bool, str]:
    """(due, why) — short history, never backfilled or last backfill > 24 h ago; a request in flight (< 10 min) waits."""
    req = (account or {}).get("h4_history_request") or {}
    dispatched = req.get("dispatched_at")
    if dispatched:
        try:
            if (now - datetime.fromisoformat(str(dispatched))).total_seconds() < REDISPATCH_SEC:
                return False, "request in flight"
        except ValueError:
            pass
    bars = completed(list((doc or {}).get("bars") or []), now.timestamp())
    if len(bars) < TARGET_BARS:
        return True, f"{len(bars)}/{TARGET_BARS} completed H4 bars stored"
    last = (doc or {}).get("backfill_at")
    if not last:
        return True, "never backfilled"
    try:
        age = (now - datetime.fromisoformat(str(last))).total_seconds()
    except ValueError:
        return True, "backfill timestamp unreadable"
    return (age >= REFRESH_SEC), ("daily refresh" if age >= REFRESH_SEC else "fresh")


def request_payload() -> dict:
    return {"symbol": SYMBOL, "timeframe": TIMEFRAME, "bars": REQUEST_BARS}


async def h4_doc(db, user_id: str) -> dict | None:
    return await db.intraday_candles.find_one(
        {"user_id": str(user_id), "symbol": SYMBOL, "timeframe": TIMEFRAME},
        {"bars": 1, "updated_at": 1, "backfill_at": 1, "account_id": 1, "t_basis": 1})


async def streams_gold(db, account: dict) -> bool:
    """Only terminals that stream XAUUSD M15 are asked (the chart/tracked symbol must exist at that broker)."""
    return bool(await db.intraday_candles.find_one(
        {"user_id": str(account.get("user_id")), "symbol": SYMBOL, "timeframe": "M15", "account_id": str(account["_id"])},
        {"_id": 1}))


async def pending_request(db, account: dict, now: datetime | None = None) -> dict | None:
    """poll-trades hook: the `candles_request` block for this terminal, or None. Marks the dispatch on the account."""
    from ea_capabilities import capabilities_for
    now = now or datetime.now(timezone.utc)
    if CAPABILITY not in capabilities_for(account.get("ea_version")):
        return None
    if not await streams_gold(db, account):
        return None
    doc = await h4_doc(db, str(account.get("user_id")))
    due, why = is_due(doc, account, now)
    if not due:
        return None
    await db.accounts.update_one({"_id": account["_id"]}, {"$set": {"h4_history_request": {
        "dispatched_at": now.isoformat(), "bars": REQUEST_BARS, "why": why}}})
    return request_payload()


async def status(db, user_id: str, account: dict | None, now: datetime | None = None) -> dict:
    """What the engine sees: merged completed H4 bars vs H4_MIN_BARS, source, backfill state, terminal capability."""
    from ea_capabilities import capabilities_for
    from gold_trend_pulse.engine import H4_MIN_BARS
    from intraday_features import load_user_candles
    from routes.bridge_routes import broker_offset_sec
    now = now or datetime.now(timezone.utc)
    m15doc = await load_user_candles(SYMBOL, user_id, "M15", last_n=800, max_age_min=24 * 60)
    m15 = list((m15doc or {}).get("bars") or [])
    h4d = await load_user_candles(SYMBOL, user_id, TIMEFRAME, last_n=400, max_age_min=DOC_MAX_AGE_SEC // 60)
    stored = completed(list((h4d or {}).get("bars") or []), now.timestamp())
    off = broker_offset_sec(account or {})[0] if (account and (m15doc or {}).get("t_basis") == "utc") else 0
    bars, source = merge_h4(stored, m15, off, now.timestamp())
    ver = (account or {}).get("ea_version")
    return {"bars": len(bars), "min_bars": H4_MIN_BARS, "warm": len(bars) >= H4_MIN_BARS, "source": source,
            "stored_ea_bars": len(stored), "backfill_at": (h4d or {}).get("backfill_at"),
            "ea_version": ver, "ea_capable": CAPABILITY in capabilities_for(ver),
            "request": (account or {}).get("h4_history_request")}


async def health_check(db, user_id: str, account: dict | None, now: datetime | None = None) -> dict:
    """Bot Health row: 'GTP: H4 bars n/200' — warm-up visible, with the reason it is not progressing."""
    st = await status(db, user_id, account, now)
    detail = f"{st['bars']}/{st['min_bars']} completed XAUUSD H4 bars ({st['source']})"
    if st["warm"]:
        return {"label": "GTP: H4 bars", "status": "pass", "detail": detail + (f" · last EA backfill {st['backfill_at']}" if st["backfill_at"] else ""),
                "fix_code": None, "fix_label": None}
    if not account:
        why = "no MT5 account streams XAUUSD"
    elif not st["ea_capable"]:
        why = f"terminal runs EA {st['ea_version'] or 'unknown'} — H4 backfill needs EA ≥ 1.66 (bias stays NONE until warm)"
    elif st["request"]:
        why = f"H4 backfill requested {st['request'].get('dispatched_at')} ({st['request'].get('why')}) — waiting for the terminal"
    else:
        why = "H4 backfill will be requested on the terminal's next poll"
    return {"label": "GTP: H4 bars", "status": "warn", "detail": f"{detail} · warm-up — {why}",
            "fix_code": None, "fix_label": None if st["ea_capable"] or not account else "Update the EA to 1.66"}
