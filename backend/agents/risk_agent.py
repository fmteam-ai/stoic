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

        others = [
            p for p in active_positions
            if (p.get("symbol") or "").upper() != sym
            and (p.get("action") or "").upper() in ("BUY", "SELL")
            and (p.get("status") or "") in ("open", "pending")
        ]
        if not others:
            return {"veto": False, "reason": "", "corr": None, "correlated_with": None}

        # iter-142 · Correlation measured on timestamp-aligned daily LOG RETURNS
        # (portfolio.var.corr_returns); unknown correlation substitutes the
        # conservative UNKNOWN_RHO prior.
        # Fix plan A8 · DIRECTION-AWARE: the exposure two positions share is
        # r × (+1 same direction, −1 opposite). BUY XAUUSD next to an open BUY
        # EURUSD with r=+0.8 stacks; BUY XAUUSD next to a SELL USDJPY with
        # r=−0.8 ALSO stacks (both bet the same way); BUY XAUUSD next to a BUY
        # USDJPY with r=−0.8 is a hedge and passes.
        from portfolio.var import UNKNOWN_RHO, corr_returns
        for p in others:
            other_sym = (p.get("symbol") or "").upper()
            other_action = (p.get("action") or "").upper()
            corr = await corr_returns(sym, other_sym)
            unknown = corr is None
            if unknown:
                corr = UNKNOWN_RHO
            sign = 1.0 if other_action == action.upper() else -1.0
            effective = corr * sign
            if effective >= self.corr_threshold:
                suffix = (" (assumed — insufficient overlapping return history)"
                          if unknown else "")
                how = "same-direction" if sign > 0 else "inverse-pair opposite-direction"
                return {
                    "veto": True,
                    "reason": (
                        f"Cross-asset exposure r={corr:+.2f} with open {other_sym} "
                        f"{other_action} → effective {effective:+.2f} ≥ "
                        f"{self.corr_threshold:+.2f} — blocking {how} stack.{suffix}"
                    ),
                    "corr": round(corr, 3),
                    "effective_corr": round(effective, 3),
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
