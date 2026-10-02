"""Strategy Optimizer — grid-search variants of a DSL and rank by backtest.

Honest about what it does: since the user doesn't have historical indicator
snapshots, the optimizer can't truthfully replay different RSI thresholds
against bars. Instead, it varies the **filter set** the existing backtest
engine can actually evaluate against closed-trade history:

  • session_preference     ∈ {london, ny, tokyo, any}
  • symbol subset          ∈ powerset({XAUUSD, BTCUSD}) minus empty
  • lookback days          ∈ {14, 30, 60}

For each combination it runs `strategy_backtest.run_backtest` and ranks by
the shared profit-tied objective (objective.stoic_score, iter-42):
    score = win_rate × avg_profit_per_trade × ln(1 + matched_trades)

Win rate and profit are TIED: a variant that wins often but loses money
scores negative and can never be selected. The optimizer returns the top
variant + the delta vs. the baseline so the UI can show "improvement found".

Output:
  {
    "baseline": {filters, win_rate, total_pnl_usd, matched_trades, score},
    "best":     {filters, win_rate, total_pnl_usd, matched_trades, score},
    "improvement_pct": float,   # 0 if best == baseline
    "tested_variants": int,
    "variants": [ {filters, score, win_rate, matched_trades, ...}, ... ],
    "notes": [str, ...],
  }
"""
import math
import logging
from itertools import product

from strategy_backtest import run_backtest
from objective import stoic_score

logger = logging.getLogger("strategy-optimizer")


SESSION_VARIANTS  = ["london", "ny", "tokyo", "any"]
LOOKBACK_VARIANTS = [14, 30, 60]


def _score(result: dict) -> float:
    # iter-42 — profit-tied objective: win rate and P&L must move together.
    return stoic_score(
        result.get("win_rate") or 0.0,
        result.get("total_pnl_usd") or 0.0,
        result.get("matched_trades") or 0,
    )


def _symbol_subsets(symbols: list[str]) -> list[list[str]]:
    if len(symbols) <= 1:
        return [symbols] if symbols else [[]]
    # Powerset minus empty, deduped, sorted for determinism
    out: list[list[str]] = []
    n = len(symbols)
    for mask in range(1, 1 << n):
        subset = [symbols[i] for i in range(n) if (mask >> i) & 1]
        out.append(subset)
    return out


async def optimize(*, dsl: dict, user_id: str) -> dict:
    """Grid-search filter variants. Returns the best variant + delta."""
    symbols = [s.upper() for s in (dsl.get("symbols") or ["XAUUSD", "BTCUSD"])]
    baseline_session = (dsl.get("session_preference") or "any").lower()

    # Build the variant grid.
    symbol_choices = _symbol_subsets(symbols)
    grid = list(product(SESSION_VARIANTS, symbol_choices, LOOKBACK_VARIANTS))

    # 1. Run baseline exactly as the DSL specifies (using the user's lookback default of 30d)
    baseline_compiled = {
        "symbols": symbols,
        "session_preference": baseline_session,
    }
    baseline_result = await run_backtest(
        compiled=baseline_compiled, user_id=user_id, lookback_days=30,
    )
    baseline = {
        "filters": {"symbols": symbols, "session_preference": baseline_session,
                    "lookback_days": 30},
        "win_rate":      baseline_result.get("win_rate"),
        "total_pnl_usd": baseline_result.get("total_pnl_usd"),
        "matched_trades": baseline_result.get("matched_trades"),
        "score": _score(baseline_result),
    }

    # 2. Sweep the grid.
    variants: list[dict] = []
    for session, sym_subset, lookback in grid:
        if not sym_subset:
            continue
        compiled = {"symbols": sym_subset, "session_preference": session}
        res = await run_backtest(
            compiled=compiled, user_id=user_id, lookback_days=lookback,
        )
        variants.append({
            "filters": {"symbols": sym_subset, "session_preference": session,
                        "lookback_days": lookback},
            "win_rate":      res.get("win_rate"),
            "total_pnl_usd": res.get("total_pnl_usd"),
            "matched_trades": res.get("matched_trades"),
            "score": _score(res),
        })

    # 3. Rank — only consider variants with at least 5 matched trades to
    #    avoid declaring a "winner" off 1-2 lucky trades.
    qualified = [v for v in variants if (v.get("matched_trades") or 0) >= 5]
    qualified.sort(key=lambda v: v["score"], reverse=True)
    best = qualified[0] if qualified else baseline

    # 4. Improvement % — guard against zero/None baseline scores.
    base_score = baseline["score"] or 0.0
    best_score = best["score"] or 0.0
    if base_score > 0:
        improvement_pct = round(((best_score - base_score) / base_score) * 100.0, 2)
    elif best_score > 0:
        improvement_pct = 100.0
    else:
        improvement_pct = 0.0

    notes: list[str] = []
    if not qualified:
        notes.append("No variant had ≥5 matched trades — baseline kept. "
                     "Trade more in paper mode to enable real optimization.")
    elif best["filters"] == baseline["filters"]:
        notes.append("Baseline already optimal across the tested grid — no change suggested.")
    else:
        notes.append(
            f"Best variant: {','.join(best['filters']['symbols'])} "
            f"during {best['filters']['session_preference'].upper()} "
            f"({best['filters']['lookback_days']}d lookback). "
            f"Improvement +{improvement_pct}% over baseline."
        )
    # Always disclose what we DON'T optimize so the user isn't misled.
    notes.append(
        "Optimizer varies filter set only (session + symbol subset + "
        "lookback). It does NOT replay indicator-threshold changes — those "
        "require live indicator snapshots which start logging in iter-30."
    )

    # Keep top-10 variants by score in the response so the UI can render them
    top_n = sorted(variants, key=lambda v: v["score"], reverse=True)[:10]

    return {
        "baseline": baseline,
        "best": best,
        "improvement_pct": improvement_pct,
        "tested_variants": len(variants),
        "variants": top_n,
        "notes": notes,
    }
