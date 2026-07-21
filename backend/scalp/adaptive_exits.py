"""Batch C · Adaptive post-entry management inside a FIXED SAFETY ENVELOPE.

Each open scalp is re-scored at most once per second from live conditions
(fresh model probability, regime permissions, volatility, spread). The layer
is strictly risk-REDUCING — the envelope guarantees it can NEVER:
  · widen the original stop        · increase exposure or lot size
  · remove broker protection       · add to a losing position
  · bypass daily loss limits (exits only realise less loss than the stop)

Deterministic actions: HOLD | TIGHTEN_STOP | EXIT_NOW.
"""

EVAL_INTERVAL_MS = 1000          # per-second re-scoring per open trade
TIGHTEN_COOLDOWN_MS = 10_000     # at most one stop-tighten per 10s per trade
P_EXIT_FLOOR = 0.35              # fresh p(target) below this → exit
VOL_TIGHTEN_RATIO = 2.5          # short/long vol expansion → tighten
SPREAD_TIGHTEN_MULT = 2.0        # spread ≥ 2× limit → tighten
TIME_DECAY_FRAC = 0.7            # past 70% of max holding …
PROGRESS_MIN_FRAC = 0.3          # … with <30% progress → tighten
MIN_STOP_GAP_PIPS = 1.0          # stop must stay this far from the market


def clamp_tighter(direction: str, current_sl: float, proposed: float,
                  mid: float, pip_size: float) -> float | None:
    """SAFETY ENVELOPE: returns a valid stop that is STRICTLY tighter than
    the current one and keeps a minimum gap from the market — or None."""
    gap = MIN_STOP_GAP_PIPS * pip_size
    if direction == "BUY":
        new = min(proposed, mid - gap)
        return new if new > current_sl else None
    new = max(proposed, mid + gap)
    return new if new < current_sl else None


def evaluate(*, direction: str, entry_px: float, mid: float, stop_px: float,
             target_px: float, pip_size: float, elapsed_ms: int,
             max_holding_ms: int, p_target: float | None = None,
             regime_opposes: bool = False, vol_ratio: float | None = None,
             spread_pips: float | None = None,
             spread_limit: float | None = None) -> dict:
    """Deterministic per-second decision for one open scalp."""
    sign = 1.0 if direction == "BUY" else -1.0
    target_dist = abs(target_px - entry_px) or pip_size
    progress = sign * (mid - entry_px) / target_dist   # 1.0 = at target
    in_profit = progress > 0

    # 1 · fresh model probability collapsed and no meaningful progress
    if p_target is not None and p_target < P_EXIT_FLOOR and progress < 0.5:
        return {"action": "EXIT_NOW", "reason": "adaptive_p_collapse",
                "p_target": round(p_target, 3), "progress": round(progress, 3)}
    # 2 · directional regime flipped against the position
    if regime_opposes and progress < 0.25:
        return {"action": "EXIT_NOW", "reason": "adaptive_regime_flip",
                "progress": round(progress, 3)}

    # tighten proposals (envelope-clamped by the caller via clamp_tighter)
    proposed = None
    reason = None
    if vol_ratio is not None and vol_ratio >= VOL_TIGHTEN_RATIO:
        reason = "adaptive_vol_shock"
        proposed = entry_px if in_profit else \
            entry_px - sign * 0.5 * abs(entry_px - stop_px)
    elif (spread_pips is not None and spread_limit
          and spread_pips >= SPREAD_TIGHTEN_MULT * spread_limit and in_profit):
        reason = "adaptive_spread_deterioration"
        proposed = entry_px
    elif (max_holding_ms and elapsed_ms >= TIME_DECAY_FRAC * max_holding_ms
          and progress < PROGRESS_MIN_FRAC):
        reason = "adaptive_time_decay"
        proposed = entry_px - sign * 0.5 * abs(entry_px - stop_px)
    if proposed is not None:
        return {"action": "TIGHTEN_STOP", "reason": reason,
                "proposed_stop_px": round(proposed, 6),
                "progress": round(progress, 3)}
    return {"action": "HOLD", "reason": None, "progress": round(progress, 3)}
