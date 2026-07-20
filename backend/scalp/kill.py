"""Scalp subsystem · Step 17 — automatic shutdown conditions.

Opening risk and reducing risk are SEPARATE permissions: HALTED still
allows closing/reconciling, it only blocks NEW entries.
"""
from scalp.state import now_ms

MAX_CLOCK_DRIFT_MS = 5000
TZ_SNAP_MS = 1_800_000        # broker clocks sit on 30-min timezone boundaries
MAX_REJECT_RATE = 0.30
SLIPPAGE_ANOMALY_MULT = 3.0
DATA_STALE_MS = 15_000


def clock_drift_residual_ms(offset_ms: float) -> float:
    """MT5 tick timestamps are broker-LOCAL time (UTC+2/+3 EET brokers,
    UTC+5:30, ...), so the measured broker↔server offset contains a large
    CONSTANT timezone component that is not drift. Snap the offset to the
    nearest 30-minute boundary and judge only the residual — genuine clock
    drift / transport skew shows up there."""
    return float(offset_ms - round(offset_ms / TZ_SNAP_MS) * TZ_SNAP_MS)


def evaluate(state, cfg) -> dict:
    reasons = []
    if state.last_tick is None:
        reasons.append("no market data")
    else:
        if state.quote_age_ms() > DATA_STALE_MS:
            reasons.append("market-data heartbeat lost")
        elif state.quote_age_ms() > cfg.max_quote_age_ms:
            reasons.append("quote stale")
        if abs(clock_drift_residual_ms(state.clock_drift_ms)) > MAX_CLOCK_DRIFT_MS:
            reasons.append("clock drift excessive")
    if state.reject_rate() > MAX_REJECT_RATE:
        reasons.append("order rejection rate elevated")
    if (state.fills_seen >= 5
            and state.slippage_ewma_pips
            > SLIPPAGE_ANOMALY_MULT * cfg.max_expected_slippage_pips):
        reasons.append("slippage exceeds forecast")
    sp = state.spread_pips()
    spreads = sorted(state.spreads)
    if sp is not None and len(spreads) >= 40:
        p97 = spreads[int(len(spreads) * 0.97)]
        if sp > max(p97, 2 * cfg.max_spread_pips):
            reasons.append("spread distribution abnormal")

    status = "OK" if not reasons else "HALTED"
    return {
        "status": status,
        "reasons": reasons,
        "open_allowed": status == "OK",
        "close_allowed": True,        # reducing risk is always permitted
        "checked_at_ms": now_ms(),
    }
