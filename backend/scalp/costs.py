"""Scalp subsystem · Step 10 — execution-cost model (pips)."""


def expected_costs(feats: dict, state, cfg, commission_pips: float = 0.0) -> dict:
    spread = float(feats["spread_pips"])
    # slippage: realized EWMA once we have fills, else the instrument cap
    slip = (state.slippage_ewma_pips if state.fills_seen >= 5
            else cfg.max_expected_slippage_pips)
    return {
        "expected_spread_cost_pips": round(spread, 2),
        "expected_slippage_pips": round(slip, 2),
        "expected_commission_pips": round(commission_pips, 2),
        # entry crosses the spread once; slippage paid on entry AND exit
        "expected_total_cost_pips": round(spread + 2 * slip + commission_pips, 2),
    }


def dynamic_spread_limit(state, cfg) -> float:
    """min(absolute cap, session p75) — Step 10."""
    spreads = sorted(state.spreads)
    if len(spreads) >= 40:
        p75 = spreads[int(len(spreads) * 0.75)]
        return min(cfg.max_spread_pips, p75)
    return cfg.max_spread_pips
