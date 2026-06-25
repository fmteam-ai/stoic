"""PortfolioAllocatorAgent — adjust a candidate signal's lot size based on
portfolio context (per-symbol volatility + recent win rate).

Blends two classical sizing schemes (config-overridable mix):
  • Volatility parity  — equal $-risk across the active universe so a
                          higher-vol symbol gets a smaller lot than a
                          lower-vol one for the same notional risk budget.
  • Kelly fraction     — scales by recent win-rate edge so symbols on a
                          winning streak get slightly more capital, losers
                          slightly less. Capped at the risk profile's
                          kelly_cap so we never blow up on a hot streak.

The agent NEVER raises the lot above the strategy's proposed size (we
trust the strategy + safety guardian for the upper bound). It only
trims when the portfolio context warrants it.

Output:
  {
    "applied": bool,
    "original_lot": float,
    "adjusted_lot": float,
    "vol_parity_scale": float,
    "kelly_scale": float,
    "blended_scale": float,
    "win_rate": float | None,
    "atr_pct": float | None,
    "reason": str,
    "bias": str,           # one-liner for the activity log
  }
"""
import logging
from datetime import datetime, timezone, timedelta

from database import get_db

logger = logging.getLogger("agent.portfolio-allocator")

# Conservative default mix — half each. Configurable via per-symbol kwargs.
DEFAULT_BLEND = {"vol_parity": 0.5, "kelly": 0.5}

# A symbol with no win-rate data gets a neutral 1.0 Kelly scale (no trim).
NEUTRAL_KELLY = 1.0
# Floor / ceiling on the final blended scale — never zero a trade out
# (the strategy/risk layer is the source of truth for hard vetoes), but
# also never inflate above 1.0× the proposed size.
MIN_SCALE = 0.25
MAX_SCALE = 1.0


def _vol_parity_scale(atr_pct_self: float | None,
                       active_atrs: list[float]) -> float:
    """If this symbol is more volatile than the active-position median,
    scale DOWN; if less volatile, return 1.0 (no inflation)."""
    if atr_pct_self is None or atr_pct_self <= 0 or not active_atrs:
        return 1.0
    # Use median for robustness — one volatile outlier won't dominate.
    s = sorted([a for a in active_atrs if a and a > 0])
    if not s:
        return 1.0
    mid = s[len(s) // 2] if len(s) % 2 else 0.5 * (s[len(s) // 2 - 1] + s[len(s) // 2])
    if mid <= 0:
        return 1.0
    return min(1.0, mid / atr_pct_self)


def _kelly_scale(win_rate: float | None, kelly_cap: float = 0.5) -> float:
    """Half-Kelly trim. Returns scale ≤ 1.0. Neutral 1.0 when no data."""
    if win_rate is None:
        return NEUTRAL_KELLY
    p = max(0.0, min(1.0, win_rate))
    # Assume R:R=1 → Kelly f* = 2p - 1. Half-Kelly for safety.
    f = (2 * p - 1) * 0.5
    # f < 0 → losing streak: trim to floor. f > kelly_cap → clamp.
    f = max(-0.5, min(kelly_cap, f))
    # Map f ∈ [-0.5, 0.5] → scale ∈ [MIN_SCALE, 1.0]
    return max(MIN_SCALE, min(1.0, 1.0 + f))


async def _recent_win_rate(db, *, user_id: str, symbol: str,
                            days: int = 30, min_n: int = 5) -> float | None:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cursor = db.trades.find({
        "user_id": user_id, "symbol": symbol.upper(),
        "status": "closed", "closed_at": {"$gte": since},
    }).limit(200)
    trades = await cursor.to_list(length=200)
    if len(trades) < min_n:
        return None
    wins = sum(1 for t in trades if float(t.get("pnl") or 0) > 0)
    return wins / len(trades)


class PortfolioAllocatorAgent:
    name = "portfolio_allocator"

    def __init__(self, blend: dict | None = None, kelly_cap: float = 0.5):
        self.blend = {**DEFAULT_BLEND, **(blend or {})}
        self.kelly_cap = kelly_cap

    async def allocate(
        self,
        *,
        user_id: str,
        signal: dict,
        technical: dict | None = None,
        active_positions: list[dict] | None = None,
    ) -> dict:
        action = (signal or {}).get("action") or "HOLD"
        if action not in ("BUY", "SELL"):
            return {"applied": False, "original_lot": signal.get("lot_size"),
                    "adjusted_lot": signal.get("lot_size"),
                    "vol_parity_scale": 1.0, "kelly_scale": 1.0,
                    "blended_scale": 1.0, "win_rate": None, "atr_pct": None,
                    "reason": "non-actionable signal — pass-through",
                    "bias": "skip (HOLD)"}

        original = float(signal.get("lot_size") or 0.0)
        if original <= 0:
            return {"applied": False, "original_lot": original,
                    "adjusted_lot": original, "vol_parity_scale": 1.0,
                    "kelly_scale": 1.0, "blended_scale": 1.0,
                    "win_rate": None, "atr_pct": None,
                    "reason": "no lot to adjust", "bias": "skip (lot=0)"}

        # Volatility-parity input: this symbol's ATR% + active positions' ATR%.
        atr_self = (technical or {}).get("atr_pct")
        active_atrs: list[float] = []
        # We don't have ATRs for active positions on-hand; use this symbol's
        # ATR vs a heuristic target. If we have multiple symbols open, use
        # them as the comparison set (only this symbol's ATR is reliable).
        # Best-effort: if active_positions present, mark presence to enable
        # the parity scaling below.
        if active_positions:
            # Use this symbol's ATR as the reference; parity scaling kicks in
            # only when the trade itself is very volatile.
            if isinstance(atr_self, (int, float)) and atr_self > 0:
                # Reference of "1.0% ATR%" as the neutral baseline (~ XAUUSD).
                active_atrs = [1.0]
        vol_scale = _vol_parity_scale(atr_self, active_atrs)

        # Kelly input: recent win rate for THIS symbol over last 30d.
        db = get_db()
        win_rate = await _recent_win_rate(db, user_id=user_id,
                                          symbol=signal.get("symbol", ""))
        kelly_scale = _kelly_scale(win_rate, kelly_cap=self.kelly_cap)

        # Blend the two scales (weighted geometric mean — multiplicative, so
        # a 0.6×0.8 blend trims more aggressively than a strict average).
        wv = self.blend.get("vol_parity", 0.5)
        wk = self.blend.get("kelly", 0.5)
        wsum = wv + wk
        if wsum <= 0:
            wv, wk, wsum = 0.5, 0.5, 1.0
        blended = max(
            MIN_SCALE,
            min(MAX_SCALE,
                (vol_scale ** (wv / wsum)) * (kelly_scale ** (wk / wsum))),
        )

        adjusted = round(original * blended, 4)
        # Honour the broker's min lot — if rounding zeroed us out, restore.
        if adjusted <= 0:
            adjusted = original

        reason_parts = []
        if vol_scale < 1.0:
            reason_parts.append(f"vol-parity trim ×{vol_scale:.2f}")
        if kelly_scale < 1.0:
            reason_parts.append(
                f"Kelly trim ×{kelly_scale:.2f} (winrate {win_rate:.0%})"
                if win_rate is not None else f"Kelly trim ×{kelly_scale:.2f}"
            )
        if not reason_parts:
            reason_parts.append("no portfolio trim applied")

        bias = (f"lot {original:.2f}→{adjusted:.2f}"
                if abs(adjusted - original) > 1e-6 else f"lot {original:.2f} kept")

        return {
            "applied": abs(adjusted - original) > 1e-6,
            "original_lot": original,
            "adjusted_lot": adjusted,
            "vol_parity_scale": round(vol_scale, 4),
            "kelly_scale": round(kelly_scale, 4),
            "blended_scale": round(blended, 4),
            "win_rate": win_rate,
            "atr_pct": atr_self,
            "reason": " · ".join(reason_parts),
            "bias": bias,
        }
