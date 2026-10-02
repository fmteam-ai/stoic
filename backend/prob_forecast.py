"""iter-64 · Probabilistic forecasting layer.

Turns Chronos decile quantiles into an explicit outcome distribution:
    62% probability: +22 pips (expected upside)
    26% probability: -10 pips (mild adverse)
    12% probability: -38 pips (severe tail)
and derives: P(up), probability-weighted EV in pips, quantile-based stop
suggestions and a dynamic lot multiplier (never scales UP, only down).

Mass model over 9 deciles q10..q90: 10% below q10, 10% in each consecutive
gap, 10% above q90; tails extrapolated by half the adjacent gap."""

from pip_utils import price_to_pips

SEVERE_TAIL_MASS = 0.10


def _segments(levels, values):
    """[(mass, midpoint_value)] over the full distribution."""
    segs = []
    lo_tail = values[0] - 0.5 * (values[1] - values[0])
    segs.append((levels[0], lo_tail))
    for i in range(len(values) - 1):
        segs.append((levels[i + 1] - levels[i], (values[i] + values[i + 1]) / 2))
    hi_tail = values[-1] + 0.5 * (values[-1] - values[-2])
    segs.append((1.0 - levels[-1], hi_tail))
    return segs


def cdf_at(x: float, levels, values) -> float:
    """P(X ≤ x) by linear interpolation of the quantile function."""
    if x <= values[0]:
        return levels[0] * max(0.0, 1 - (values[0] - x) / max(values[1] - values[0], 1e-9))
    if x >= values[-1]:
        return 1.0 - (1.0 - levels[-1]) * max(
            0.0, 1 - (x - values[-1]) / max(values[-1] - values[-2], 1e-9))
    for i in range(len(values) - 1):
        if values[i] <= x <= values[i + 1]:
            span = max(values[i + 1] - values[i], 1e-9)
            return levels[i] + (levels[i + 1] - levels[i]) * (x - values[i]) / span
    return 0.5


def scenario_table(symbol: str, last: float, levels, values) -> dict:
    """3-bucket outcome distribution in pips (the user-facing format)."""
    p_down = cdf_at(last, levels, values)
    p_up = 1.0 - p_down
    up_m = up_p = mild_m = mild_p = sev_m = sev_p = 0.0
    cum = 0.0
    for mass, mid in _segments(levels, values):
        pips = price_to_pips(symbol, mid - last)
        if mid > last:
            up_m += mass; up_p += mass * pips
        elif cum < SEVERE_TAIL_MASS:
            sev_m += mass; sev_p += mass * pips
        else:
            mild_m += mass; mild_p += mass * pips
        cum += mass
    scenarios = []
    if up_m > 0:
        scenarios.append({"prob": round(up_m, 2), "pips": round(up_p / up_m, 1),
                          "label": "upside"})
    if mild_m > 0:
        scenarios.append({"prob": round(mild_m, 2), "pips": round(mild_p / mild_m, 1),
                          "label": "mild_adverse"})
    if sev_m > 0:
        scenarios.append({"prob": round(sev_m, 2), "pips": round(sev_p / sev_m, 1),
                          "label": "severe_tail"})
    ev = sum(s["prob"] * s["pips"] for s in scenarios)
    return {"p_up": round(p_up, 3), "scenarios": scenarios,
            "ev_pips_long": round(ev, 1)}


def trade_eval(symbol: str, action: str, entry, sl, tp1, fc: dict) -> dict | None:
    """Probability-weighted evaluation of a concrete trade plan."""
    if action not in ("BUY", "SELL") or not fc or not fc.get("quantile_values"):
        return None
    levels, values = fc["quantile_levels"], fc["quantile_values"]
    # Conformalised deciles (conformal.py · ACI) when this user+symbol's
    # coverage state is mature — realised coverage of the outer band then
    # tracks its nominal level. Otherwise the raw deciles, as before.
    conf = fc.get("conformal") or {}
    conformal_used = bool(conf.get("active") and conf.get("quantile_values")
                          and len(conf["quantile_values"]) == len(values))
    if conformal_used:
        levels, values = conf["quantile_levels"], conf["quantile_values"]
    last = fc["last"]
    try:
        entry, sl, tp1 = float(entry), float(sl), float(tp1)
    except (TypeError, ValueError):
        return None
    sign = 1.0 if action == "BUY" else -1.0
    ev_pips = sign * (scenario_table(symbol, last, levels, values)["ev_pips_long"])
    if action == "BUY":
        p_tp = 1.0 - cdf_at(tp1, levels, values)   # P(end beyond TP1)
        p_sl = cdf_at(sl, levels, values)          # P(end beyond SL)
        suggested_sl = values[0]                   # q10 — only 10% of paths below
        # must stay a real stop: tighter than the current SL AND below entry
        tighter = sl < suggested_sl < entry
    else:
        p_tp = cdf_at(tp1, levels, values)
        p_sl = 1.0 - cdf_at(sl, levels, values)
        suggested_sl = values[-1]                  # q90
        tighter = entry < suggested_sl < sl
    tp_pips = abs(price_to_pips(symbol, tp1 - entry))
    sl_pips = abs(price_to_pips(symbol, sl - entry))
    prob_expectancy = p_tp * tp_pips - p_sl * sl_pips
    if ev_pips > 0 and prob_expectancy > 0:
        lot_multiplier = 1.0
    elif ev_pips > 0 or prob_expectancy > 0:
        lot_multiplier = 0.75
    else:
        lot_multiplier = 0.5
    return {
        "ev_pips": round(ev_pips, 1),
        "p_tp": round(p_tp, 3), "p_sl": round(p_sl, 3),
        "prob_expectancy_pips": round(prob_expectancy, 1),
        "suggested_sl": round(suggested_sl, 5) if tighter else None,
        "lot_multiplier": lot_multiplier,
        "negative_ev": bool(ev_pips <= 0 and prob_expectancy <= 0),
        "conformal": conformal_used,
    }
