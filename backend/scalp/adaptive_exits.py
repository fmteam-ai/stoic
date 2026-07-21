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


def position_p_target(*, direction: str, entry_px: float, mid: float,
                      stop_px: float, target_px: float, pip_size: float,
                      p_model: float | None = None, elapsed_ms: int = 0,
                      max_holding_ms: int = 0,
                      vol_short_pips: float | None = None,
                      spread_pips: float | None = None,
                      mfe_frac: float | None = None,
                      mae_frac: float | None = None) -> float:
    """Round 18 review item 7 — P(THIS target is reached before THIS stop
    within the REMAINING holding horizon) for an EXISTING position.

    A fresh-entry model score is NOT that probability. Composite:
    · driftless double-barrier base:  dist_stop / (dist_stop + dist_target)
    · drift tilt from the calibrated model p (entry-style directional edge)
    · time-capacity discount: expected reachable range from remaining time ×
      short-horizon volatility vs the remaining distance to target (+ spread)
    · MAE/MFE penalty: deep adverse excursion with no favorable progress
    """
    sign = 1.0 if direction == "BUY" else -1.0
    dist_t = sign * (target_px - mid) / pip_size    # pips still to travel
    dist_s = sign * (mid - stop_px) / pip_size      # pips of room to stop
    if dist_t <= 0:
        return 0.98                                 # at/through target
    if dist_s <= 0:
        return 0.02                                 # at/through stop
    p = dist_s / (dist_s + dist_t)
    if p_model is not None:
        p += 0.6 * (p_model - 0.5)
    if max_holding_ms and vol_short_pips:
        remaining_min = max(0.0, (max_holding_ms - elapsed_ms) / 60_000.0)
        reach = max(float(vol_short_pips), 0.1) * (remaining_min ** 0.5)
        need = dist_t + (spread_pips or 0.0)
        p *= max(0.3, min(1.0, reach / need))
    if mae_frac is not None and mae_frac >= 0.7 and (mfe_frac or 0.0) < 0.2:
        p *= 0.8
    return round(min(0.98, max(0.02, p)), 4)


def evaluate(*, direction: str, entry_px: float, mid: float, stop_px: float,
             target_px: float, pip_size: float, elapsed_ms: int,
             max_holding_ms: int, p_target: float | None = None,
             regime_opposes: bool = False, vol_ratio: float | None = None,
             spread_pips: float | None = None,
             spread_limit: float | None = None) -> dict:
    """Deterministic per-second decision for one open scalp.

    p_target MUST be the position-conditioned probability from
    position_p_target() — never a fresh-entry model score."""
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
