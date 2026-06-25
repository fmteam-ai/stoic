"""TechnicalAnalysisAgent — focused indicator + regime view of one symbol.

Pure-deterministic: pulls history + indicators, classifies regime, returns
a compact verdict the StrategyAgent can fold into its prompt. No LLM,
no network beyond the existing cached `get_history`.

Output:
  {
    "symbol": str,
    "trend": "UP" | "DOWN" | "FLAT",
    "regime": str,            # microstructure label (trending/ranging/volatile)
    "rsi": float | None,
    "rsi_extreme": "OVERBOUGHT"|"OVERSOLD"|None,
    "atr_pct": float | None,  # ATR as % of price — vol proxy
    "session": dict,          # current session window
    "bias": str,              # one-line directional summary
    "indicators": dict,       # full indicator dict (passthrough for Strategy)
  }
"""
import logging

from market import get_history, compute_indicators
from microstructure import current_session, session_bias_for, classify_regime

logger = logging.getLogger("agent.technical")


class TechnicalAnalysisAgent:
    name = "technical"

    async def analyze(self, symbol: str) -> dict:
        sym = symbol.upper()
        history = await get_history(sym)
        indicators = compute_indicators(history) or {}
        regime = classify_regime(indicators)
        session = current_session()

        price = float(indicators.get("current_price") or 0.0)
        ma20 = float(indicators.get("ma_20") or 0.0)
        ma200 = float(indicators.get("ma_200") or 0.0)
        rsi = indicators.get("rsi_14")
        atr = float(indicators.get("atr_14") or 0.0)

        # Deterministic trend label: MA20 vs MA200 with a small dead-band.
        trend = "FLAT"
        if ma20 and ma200:
            spread_pct = (ma20 - ma200) / ma200 * 100.0
            if spread_pct > 0.5:
                trend = "UP"
            elif spread_pct < -0.5:
                trend = "DOWN"

        rsi_extreme = None
        if isinstance(rsi, (int, float)):
            if rsi >= 70:
                rsi_extreme = "OVERBOUGHT"
            elif rsi <= 30:
                rsi_extreme = "OVERSOLD"

        atr_pct = (atr / price * 100.0) if price and atr else None

        # Single-line bias for the activity log
        bias_parts = [f"trend={trend}", f"regime={regime}"]
        if rsi is not None:
            bias_parts.append(f"RSI={rsi:.0f}")
        if rsi_extreme:
            bias_parts.append(rsi_extreme)
        if atr_pct is not None:
            bias_parts.append(f"ATR%={atr_pct:.2f}")

        return {
            "symbol": sym,
            "trend": trend,
            "regime": regime,
            "rsi": rsi,
            "rsi_extreme": rsi_extreme,
            "atr_pct": atr_pct,
            "session": {**session, **session_bias_for(sym, session)},
            "bias": " · ".join(bias_parts),
            "indicators": indicators,
        }
