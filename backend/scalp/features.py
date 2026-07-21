"""Scalp subsystem · Step 3 — incremental feature snapshot.

All returns are expressed in PIPS. Feature keys are the model contract:
scalp/model.py trains on exactly FEATURE_KEYS order — change both together.
"""
import math

from scalp.state import ScalpState
from scalp.feature_schema import current_keys

FEATURE_KEYS = list(current_keys())


def _mid_at(ticks, target_ms: int) -> float | None:
    """Most recent mid at-or-before target_ms (linear scan from right)."""
    for i in range(len(ticks) - 1, -1, -1):
        if ticks[i][0] <= target_ms:
            return ticks[i][3]
    return None


def _ret_pips(ticks, now_ms_: int, window_ms: int, pip: float) -> float | None:
    if not ticks:
        return None
    past = _mid_at(ticks, now_ms_ - window_ms)
    if past is None:
        return None
    return (ticks[-1][3] - past) / pip


def _vol_pips(ticks, now_ms_: int, window_ms: int, step_ms: int, pip: float):
    """Std of step returns over the window, in pips."""
    rets = []
    t = now_ms_ - window_ms
    prev = _mid_at(ticks, t)
    while t < now_ms_ and prev is not None:
        t += step_ms
        cur = _mid_at(ticks, t)
        if cur is None:
            break
        rets.append((cur - prev) / pip)
        prev = cur
    if len(rets) < 5:
        return None
    m = sum(rets) / len(rets)
    return math.sqrt(sum((r - m) ** 2 for r in rets) / len(rets))


def snapshot(state: ScalpState) -> dict | None:
    """Feature dict at the latest tick, or None when history is too thin."""
    if state.last_tick is None or len(state.ticks) < 20:
        return None
    ticks = state.ticks
    now = ticks[-1][0]
    pip = state.pip
    span_ms = now - ticks[0][0]
    if span_ms < 35_000:          # need ≥35s of ticks for ret_30s
        return None

    ret_1s = _ret_pips(ticks, now, 1_000, pip)
    ret_3s = _ret_pips(ticks, now, 3_000, pip)
    ret_5s = _ret_pips(ticks, now, 5_000, pip)
    ret_10s = _ret_pips(ticks, now, 10_000, pip)
    ret_30s = _ret_pips(ticks, now, 30_000, pip)
    if None in (ret_1s, ret_3s, ret_5s, ret_10s, ret_30s):
        return None
    # acceleration: this second's move minus the prior second's move
    prev_1s_mid = _mid_at(ticks, now - 1_000)
    prev_2s_mid = _mid_at(ticks, now - 2_000)
    accel = None
    if prev_1s_mid is not None and prev_2s_mid is not None:
        accel = ret_1s - (prev_1s_mid - prev_2s_mid) / pip
    vol_short = _vol_pips(ticks, now, 60_000, 1_000, pip)
    vol_long = _vol_pips(ticks, now, 300_000, 5_000, pip)

    n_recent = 0
    ups = downs = 0
    prev_mid = None
    for tm, _, _, mid in ticks:
        if tm < now - 30_000:
            continue
        if prev_mid is not None:
            if mid > prev_mid:
                ups += 1
            elif mid < prev_mid:
                downs += 1
        prev_mid = mid
        n_recent += 1
    uptick_ratio = ups / max(ups + downs, 1)

    spread = state.spread_pips()
    spreads = sorted(state.spreads)
    pctl = (sum(1 for s in spreads if s <= spread) / len(spreads)) if spreads else 1.0

    ticks_10s = sum(1 for tm, *_ in ticks if tm >= now - 10_000)

    return {
        "ret_1s": ret_1s, "ret_3s": ret_3s, "ret_5s": ret_5s,
        "ret_10s": ret_10s, "ret_30s": ret_30s,
        "accel": accel if accel is not None else 0.0,
        "vwap_dist": (ticks[-1][3] - state.vwap_ewma) / pip,
        "vol_short": vol_short if vol_short is not None else 0.0,
        "vol_long": vol_long if vol_long is not None else 0.0,
        "tick_rate": ticks_10s / 10.0,
        "spread_pips": spread,
        "spread_pctl": round(pctl, 3),
        "uptick_ratio": round(uptick_ratio, 3),
        "time_since_change_s": (now - state.last_price_change_ms) / 1000.0,
        "_now_ms": now,
        "_bid": state.last_tick.bid,
        "_ask": state.last_tick.ask,
    }
