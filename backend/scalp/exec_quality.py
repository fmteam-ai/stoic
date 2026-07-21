"""Refinements 1+2 · Execution Quality Score + adaptive min-edge threshold.

Execution quality is scored 0-100 immediately before live submission from
observable execution conditions (NOT signal quality — that's trade_quality).
Poor conditions GATE the trade (user decision): score < EXEC_QUALITY_MIN
skips submission even when the expected value is positive.

The minimum net edge adapts to conditions within [EDGE_FLOOR_PIPS,
EDGE_CEIL_PIPS] — never looser than the historical 0.15p floor, up to 4×
stricter when volatility/spread/execution/loss-streak deteriorate.
"""

EXEC_QUALITY_MIN = 40          # gate: live submissions below this are skipped
EDGE_FLOOR_PIPS = 0.15         # never looser than the historical constant
EDGE_CEIL_PIPS = 0.60

# review item 4 — FAIL-CLOSED history policy (strict): with fewer than
# MIN_BROKER_FILLS_FOR_LIVE real fills for this broker, the execution score
# is capped BELOW the gate so live trades are blocked until the bot has
# real execution evidence (demo-live fills count). 20-100 fills → blended
# conservative prior (see blend()); >100 → empirical.
MIN_BROKER_FILLS_FOR_LIVE = 20
FULL_EMPIRICAL_FILLS = 100
INSUFFICIENT_HISTORY_MAX_SCORE = 39

# review item 6 — every decision stores the thresholds version so the gate
# can later be calibrated empirically against realised outcomes.
THRESHOLDS_VERSION = 1

# additive weights — sum to 100
EQ_WEIGHTS = {
    "spread": 22, "quote_age": 20, "slippage": 20,
    "latency": 14, "volatility": 14, "session": 10,
}


def thresholds_snapshot() -> dict:
    return {
        "version": THRESHOLDS_VERSION,
        "exec_quality_min": EXEC_QUALITY_MIN,
        "edge_floor_pips": EDGE_FLOOR_PIPS,
        "edge_ceil_pips": EDGE_CEIL_PIPS,
        "min_broker_fills_for_live": MIN_BROKER_FILLS_FOR_LIVE,
        "full_empirical_fills": FULL_EMPIRICAL_FILLS,
        "quote_age_full_ms": 500, "quote_age_zero_ms": 2500,
        "weights": dict(EQ_WEIGHTS),
    }


def blend(local: float | None, prior: float | None,
          local_n: int) -> float | None:
    """review item 5 — weighted blend of fresh local telemetry with the
    persisted broker/session prior; weighting favours recent live data as
    local samples accumulate (full local trust at 20 samples)."""
    if local is None and prior is None:
        return None
    if local is None:
        return prior
    if prior is None:
        return local
    w = min(1.0, max(0, int(local_n)) / 20.0)
    return w * float(local) + (1 - w) * float(prior)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def session_name(hour_utc: int) -> str:
    h = int(hour_utc) % 24
    if 7 <= h < 12:
        return "london"
    if 12 <= h < 16:
        return "overlap"
    if 16 <= h < 20:
        return "newyork"
    if 20 <= h < 24:
        return "late"
    return "asia"


def execution_quality(*, spread_pctl: float | None = None,
                      quote_age_ms: float | None = None,
                      avg_slippage_pips: float | None = None,
                      ack_latency_ms: float | None = None,
                      vol_ratio: float | None = None,
                      hour_utc: int | None = None,
                      broker_fill_count: int | None = None,
                      history_cap_exempt: bool = False) -> dict:
    """0-100 additive score; None inputs get neutral half-credit, BUT with
    insufficient real broker fills the final score is capped below the live
    gate (fail-closed — half-credit neutrality alone must never pass a live
    trade with no execution evidence)."""
    b: dict[str, float] = {}
    b["spread"] = (EQ_WEIGHTS["spread"] * 0.5 if spread_pctl is None else
                   (1.0 - _clamp(float(spread_pctl), 0, 1))
                   * EQ_WEIGHTS["spread"])
    if quote_age_ms is None:
        b["quote_age"] = EQ_WEIGHTS["quote_age"] * 0.5
    else:
        # ≤500ms full credit → 2500ms (max allowed) zero
        b["quote_age"] = (1.0 - _clamp((float(quote_age_ms) - 500) / 2000,
                                       0, 1)) * EQ_WEIGHTS["quote_age"]
    if avg_slippage_pips is None:
        b["slippage"] = EQ_WEIGHTS["slippage"] * 0.5
    else:
        # ≤0.1p full credit → 0.6p+ zero
        b["slippage"] = (1.0 - _clamp((abs(float(avg_slippage_pips)) - 0.1)
                                      / 0.5, 0, 1)) * EQ_WEIGHTS["slippage"]
    if ack_latency_ms is None:
        b["latency"] = EQ_WEIGHTS["latency"] * 0.5
    else:
        # ≤1s full → 8s+ zero
        b["latency"] = (1.0 - _clamp((float(ack_latency_ms) - 1000) / 7000,
                                     0, 1)) * EQ_WEIGHTS["latency"]
    if vol_ratio is None:
        b["volatility"] = EQ_WEIGHTS["volatility"] * 0.5
    else:
        # calm (≤1.2×) full → 3×+ expansion zero
        b["volatility"] = (1.0 - _clamp((max(0.0, float(vol_ratio)) - 1.2)
                                        / 1.8, 0, 1)) * EQ_WEIGHTS["volatility"]
    if hour_utc is None:
        b["session"] = EQ_WEIGHTS["session"] * 0.5
    else:
        s = session_name(hour_utc)
        b["session"] = EQ_WEIGHTS["session"] * {
            "london": 1.0, "overlap": 1.0, "newyork": 0.8,
            "late": 0.3, "asia": 0.4}[s]
    breakdown = {k: round(v, 1) for k, v in b.items()}
    score = int(round(_clamp(sum(b.values()), 0, 100)))
    fills = int(broker_fill_count or 0)
    # demo-live is exempt: risk-free fills are HOW the history gets built
    history_capped = (not history_cap_exempt
                      and fills < MIN_BROKER_FILLS_FOR_LIVE)
    if history_capped:
        score = min(score, INSUFFICIENT_HISTORY_MAX_SCORE)
    return {"score": score, "breakdown": breakdown,
            "broker_fill_count": fills, "history_capped": history_capped,
            "gate_min": EXEC_QUALITY_MIN,
            "thresholds_version": THRESHOLDS_VERSION, "version": 1}


def adaptive_min_edge(*, exec_score: float | None = None,
                      vol_ratio: float | None = None,
                      spread_pctl: float | None = None,
                      loss_streak: int = 0) -> dict:
    """Condition-scaled minimum net edge in [0.15p, 0.60p]."""
    comps: dict[str, float] = {"base": EDGE_FLOOR_PIPS}
    if exec_score is not None and exec_score < 70:
        comps["execution"] = round((70 - float(exec_score)) / 100 * 0.20, 3)
    if vol_ratio is not None:
        vr = float(vol_ratio)
        if vr > 2.5:
            comps["volatility"] = 0.20
        elif vr > 1.5:
            comps["volatility"] = 0.10
    if spread_pctl is not None and float(spread_pctl) > 0.8:
        comps["spread_regime"] = 0.10
    if int(loss_streak or 0) >= 2:
        comps["loss_streak"] = round(min(0.15, 0.05 * int(loss_streak)), 3)
    threshold = round(_clamp(sum(comps.values()),
                             EDGE_FLOOR_PIPS, EDGE_CEIL_PIPS), 3)
    return {"min_edge_pips": threshold, "components": comps, "version": 1}
