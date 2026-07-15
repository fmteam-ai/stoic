"""Scalp subsystem · Step 5 — conservative net-edge decision (deterministic)."""

MIN_NET_EDGE_PIPS = 0.15
MAX_COST_FRACTION = 0.25     # Step 10: cost ≤ 25% of expected gross move


def evaluate(forecast) -> dict:
    cost = forecast.expected_total_cost_pips if hasattr(
        forecast, "expected_total_cost_pips") else (
        forecast.expected_spread_cost_pips
        + 2 * forecast.expected_slippage_pips
        + forecast.expected_commission_pips)
    net_edge = (forecast.p_target_before_stop * forecast.target_pips
                - (1.0 - forecast.p_target_before_stop) * forecast.stop_pips
                - cost
                - forecast.uncertainty_pips)
    gross = forecast.p_target_before_stop * forecast.target_pips
    cost_ratio = cost / gross if gross > 0 else 999.0
    ok = net_edge >= MIN_NET_EDGE_PIPS and cost_ratio <= MAX_COST_FRACTION
    reason = None
    if net_edge < MIN_NET_EDGE_PIPS:
        reason = f"net_edge {net_edge:.2f}p < {MIN_NET_EDGE_PIPS}p"
    elif cost_ratio > MAX_COST_FRACTION:
        reason = f"cost consumes {cost_ratio:.0%} of expected alpha (cap {MAX_COST_FRACTION:.0%})"
    return {"ok": ok, "net_edge_pips": round(net_edge, 3),
            "cost_pips": round(cost, 2), "cost_ratio": round(cost_ratio, 3),
            "reason": reason}
