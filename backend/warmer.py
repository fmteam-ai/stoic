"""Background data warmer tasks.

Pre-fetches slow or rate-limited external data on a schedule so live trading
paths read from a hot in-process cache (microsecond access) instead of triggering
network requests in the hot path.

Currently warms:
  - Forex Factory economic calendar (every 6 hours)
  - Market history for default symbols (every 4 hours)
"""
import asyncio
import logging

from economic_calendar import _fetch_events, _cache as cal_cache
from market import get_history, SYMBOL_MAP
from execution import settle_paper_trades_against_price
import time

logger = logging.getLogger("warmer")

DEFAULT_WARM_SYMBOLS = ["XAUUSD", "BTCUSD"]
CALENDAR_INTERVAL_SEC = 6 * 3600   # every 6h
HISTORY_INTERVAL_SEC = 4 * 3600    # every 4h


async def _warm_calendar_once():
    try:
        events = await _fetch_events()
        cal_cache["events"] = events
        cal_cache["expires_at"] = time.time() + CALENDAR_INTERVAL_SEC
        logger.info("Calendar warmed: %d events cached for 6h", len(events))
    except Exception as e:
        logger.warning("Calendar warm failed: %s", e)


async def _warm_history_once(symbols=None):
    symbols = symbols or DEFAULT_WARM_SYMBOLS
    for sym in symbols:
        if sym not in SYMBOL_MAP:
            continue
        try:
            await get_history(sym)
            logger.debug("History warmed for %s", sym)
        except Exception as e:
            logger.warning("History warm failed for %s: %s", sym, e)
        await asyncio.sleep(2)  # gentle pacing to dodge rate limits


async def loop():
    """Long-running coroutine — warms calendar + history on independent schedules."""
    logger.info("Warmer task started")
    # Initial warm
    await _warm_calendar_once()
    await _warm_history_once()
    cal_next = time.time() + CALENDAR_INTERVAL_SEC
    hist_next = time.time() + HISTORY_INTERVAL_SEC
    while True:
        await asyncio.sleep(60)
        now = time.time()
        # Continuously settle paper trades against live mid-price
        try:
            closed = await settle_paper_trades_against_price()
            if closed:
                logger.info("Settled %d paper trades", closed)
        except Exception as e:
            logger.warning("Paper settlement failed: %s", e)
        if now >= cal_next:
            await _warm_calendar_once()
            cal_next = now + CALENDAR_INTERVAL_SEC
        if now >= hist_next:
            await _warm_history_once()
            hist_next = now + HISTORY_INTERVAL_SEC
