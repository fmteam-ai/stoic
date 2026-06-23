"""ResearchAgent — gathers unstructured + macro signals for the current symbol.

Sources:
  - News sentiment (NewsAPI)            — existing `news.score_sentiment`
  - COT positioning (XAU only)          — existing `macro.cot`
  - TIPS real yield (XAU only)          — existing `macro.tips`
  - DXY snapshot (XAU only)             — existing `macro.dxy`
  - Economic-calendar imminent events   — existing `economic_calendar`
  - **NEW**: FRED macro snapshot        — `macro.fred` (5 series, 6h cache)

Output contract: a single dict the StrategyAgent can fold into the Claude prompt.
"""
import logging

from news import score_sentiment
from economic_calendar import macro_freeze_check, upcoming_for
from macro.cot import get_gold_positioning
from macro.tips import get_real_yield
from macro.dxy import get_dxy_snapshot
from macro.fred import get_macro_snapshot as get_fred_snapshot

logger = logging.getLogger("agent.research")


class ResearchAgent:
    """Gather macro + unstructured inputs for one symbol on one tick."""

    name = "research"

    async def gather(self, symbol: str) -> dict:
        sym = symbol.upper()
        sentiment = await score_sentiment(symbol)
        macro_freeze = await macro_freeze_check(symbol)
        upcoming = await upcoming_for(symbol, hours=24)

        cot = tips = dxy = None
        if sym == "XAUUSD":
            try:
                cot = await get_gold_positioning()
            except Exception as e:
                logger.debug("COT fetch failed: %s", e)
            try:
                tips = await get_real_yield()
            except Exception as e:
                logger.debug("TIPS fetch failed: %s", e)
            try:
                dxy = await get_dxy_snapshot()
            except Exception as e:
                logger.debug("DXY fetch failed: %s", e)

        fred = None
        try:
            fred = await get_fred_snapshot()
        except Exception as e:
            logger.debug("FRED fetch failed: %s", e)

        return {
            "symbol": sym,
            "sentiment": sentiment,
            "macro_freeze": macro_freeze,
            "upcoming_macro": upcoming[:5],
            "cot_positioning": cot,
            "real_yield_10y": tips,
            "dxy": dxy,
            "fred": fred,
        }
