"""STOIC unified learning objective — iter-42.

Every component that learns, ranks or adapts (auto-tune thresholds, strategy
optimizer grid search, adaptive risk multiplier, AI Strategy Optimizer) must
optimize WIN RATE and PROFIT **together**, never one at the expense of the
other. This module is the single source of truth for that objective.

Core ideas:
  · expectancy_r = p × payoff − (1 − p)      (edge per trade in R units)
    Positive only when the win rate AND the payoff ratio jointly clear the
    breakeven curve — a 90% win rate with payoff 0.05 scores NEGATIVE.
  · stoic_score  = win_rate × avg_profit_per_trade × ln(1 + n)
    Ranking metric for grid searches: profit per trade weighted by how often
    it wins, with a sample-size confidence kicker. Net-losing variants score
    negative regardless of win rate.
"""
import math


def expectancy_stats(pnls: list) -> dict:
    """Digest a list of realized P&Ls into the joint win-rate/profit view."""
    pnls = [float(p or 0) for p in pnls]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    decided = len(wins) + len(losses)
    win_rate = round(100.0 * len(wins) / decided, 1) if decided else None
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    payoff = round(avg_win / abs(avg_loss), 2) if avg_loss else None

    expectancy_r = None
    if decided:
        p = len(wins) / decided
        if payoff is not None:
            expectancy_r = round(p * payoff - (1 - p), 3)
        elif wins:                      # no losses at all — pure edge
            expectancy_r = round(p, 3)

    return {
        "win_rate": win_rate,
        "payoff_ratio": payoff,
        "expectancy_r": expectancy_r,
        "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
        "total_pnl": round(sum(pnls), 2),
        "decided": decided,
    }


def stoic_score(win_rate_pct: float, total_pnl: float, n: int) -> float:
    """Profit-tied ranking score. Higher only when trades win often AND make
    money; a losing variant is negative no matter how high its win rate."""
    if not n or n <= 0:
        return 0.0
    return ((win_rate_pct or 0.0) / 100.0) * (float(total_pnl or 0.0) / n) * math.log1p(n)
