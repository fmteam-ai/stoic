"""RiskAgent — automated compliance officer.

The 10-layer veto cascade inside `ai_signals.analyze_symbol` already handles
per-signal vetoes. The RiskAgent adds **portfolio-level** checks that the
per-signal cascade can't see:

  - cross_asset_correlation: if a same-direction trade is already open or
    pending on a correlated symbol (XAU↔BTC under macro stress), reject the
    new signal to avoid accidental double-leverage on the same driver.

Future hooks (out of scope this iteration):
  - basket exposure limits (max USD exposure across all open positions)
  - duration / time-of-day caps
  - news-velocity throttle
"""
import logging
from typing import Iterable

from market import get_history

logger = logging.getLogger("agent.risk")

DEFAULT_CORR_THRESHOLD = 0.7
DEFAULT_CORR_LOOKBACK_BARS = 30


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    xs = xs[-n:]
    ys = ys[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = sum((xs[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((ys[i] - my) ** 2 for i in range(n)) ** 0.5
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


async def _close_series(symbol: str, n: int) -> list[float]:
    try:
        hist = await get_history(symbol)
    except Exception:
        return []
    closes = [c.get("close") for c in (hist or []) if c.get("close") is not None]
    return list(closes[-n:])


class RiskAgent:
    name = "risk"

    def __init__(self, corr_threshold: float = DEFAULT_CORR_THRESHOLD,
                 lookback_bars: int = DEFAULT_CORR_LOOKBACK_BARS):
        self.corr_threshold = corr_threshold
        self.lookback_bars = lookback_bars

    async def cross_asset_correlation_veto(
        self,
        symbol: str,
        action: str,
        active_positions: Iterable[dict],
    ) -> dict:
        """Returns {'veto': bool, 'reason': str, 'corr': float|None,
                    'correlated_with': str|None}.

        Triggers when:
          - There's already an OPEN or PENDING same-direction trade on a
            different symbol AND
          - The two symbols' price series are correlated above the threshold.
        """
        sym = symbol.upper()
        if action not in ("BUY", "SELL"):
            return {"veto": False, "reason": "", "corr": None, "correlated_with": None}

        same_dir_other = [
            p for p in active_positions
            if (p.get("symbol") or "").upper() != sym
            and (p.get("action") or "").upper() == action.upper()
            and (p.get("status") or "") in ("open", "pending")
        ]
        if not same_dir_other:
            return {"veto": False, "reason": "", "corr": None, "correlated_with": None}

        sym_closes = await _close_series(sym, self.lookback_bars)
        if len(sym_closes) < 3:
            return {"veto": False, "reason": "", "corr": None, "correlated_with": None}

        for p in same_dir_other:
            other_sym = (p.get("symbol") or "").upper()
            other_closes = await _close_series(other_sym, self.lookback_bars)
            if len(other_closes) < 3:
                continue
            corr = _pearson(sym_closes, other_closes)
            if abs(corr) >= self.corr_threshold:
                return {
                    "veto": True,
                    "reason": (
                        f"Cross-asset correlation r={corr:+.2f} ≥ "
                        f"{self.corr_threshold:+.2f} with open {other_sym} "
                        f"{action} — blocking same-direction stack."
                    ),
                    "corr": round(corr, 3),
                    "correlated_with": other_sym,
                }
        return {"veto": False, "reason": "", "corr": None, "correlated_with": None}

    async def review(
        self,
        symbol: str,
        signal: dict,
        active_positions: Iterable[dict],
    ) -> dict:
        """Apply portfolio-level checks on top of the per-signal cascade.

        Returns {'approved': bool, 'signal': dict, 'overrides': [...]}.
        Mutates `signal` minimally — only adds a `risk_agent_veto` block and
        flips `action`/`tradeable` to HOLD if a portfolio veto fires.
        """
        action = (signal or {}).get("action") or "HOLD"
        overrides: list[dict] = []

        corr = await self.cross_asset_correlation_veto(symbol, action, active_positions)
        signal["cross_asset_correlation"] = corr  # always surface for transparency
        if corr["veto"]:
            signal["action"] = "HOLD"
            signal["tradeable"] = False
            signal["risk_agent_veto"] = corr["reason"]
            existing = signal.get("reasoning") or ""
            signal["reasoning"] = f"{existing}\n\nVETO (cross-asset correlation): {corr['reason']}"
            overrides.append({
                "kind": "cross_asset_correlation",
                "reason": corr["reason"],
                "details": corr,
            })

        return {
            "approved": signal.get("action") in ("BUY", "SELL") and bool(signal.get("tradeable")),
            "signal": signal,
            "overrides": overrides,
        }
