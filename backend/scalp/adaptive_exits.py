"""Phase B · Trade Management Engine — adaptive post-entry management inside
a FIXED SAFETY ENVELOPE.

Each open scalp is re-scored at most once per second from live conditions
(position-conditioned probability, regime permissions, volatility, spread,
liquidity). The layer is strictly risk-REDUCING — the envelope guarantees it
can NEVER:
  · widen the original stop        · increase exposure or lot size
  · remove broker protection       · add to a losing position
  · extend the target beyond the original TP
  · bypass daily loss limits (exits only realise less loss than the stop)

Deterministic actions, priority-ordered:
    EXIT_NOW > PARTIAL_CLOSE > TIGHTEN_STOP > TIGHTEN_TP > HOLD
"""

EVAL_INTERVAL_MS = 1000          # per-second re-scoring per open trade
TIGHTEN_COOLDOWN_MS = 10_000     # at most one stop-tighten per 10s per trade
TP_ADJUST_COOLDOWN_MS = 30_000   # at most one TP adjustment per 30s per trade
P_EXIT_FLOOR = 0.35              # position p(target) below this → exit
VOL_TIGHTEN_RATIO = 2.5          # short/long vol expansion → tighten
SPREAD_TIGHTEN_MULT = 2.0        # spread ≥ 2× limit → tighten
TIME_DECAY_FRAC = 0.7            # past 70% of max holding …
PROGRESS_MIN_FRAC = 0.3          # … with <30% progress → tighten
MIN_STOP_GAP_PIPS = 1.0          # stop must stay this far from the market

# Phase B — trade-management thresholds
EV_EXIT_R = -0.15                # hold-EV (in R of stop) below this → exit
EV_MIN_PROFIT_R = 0.15           # EV exits only lock gains, never panic-cut
PARTIAL_TRIGGER_R = 0.7          # defensive partial arms at +0.7R …
PARTIAL_P_MAX = 0.5              # … when p(target) has sunk below 0.5
PARTIAL_FRACTION = 0.5           # close half, let the rest work
TP_TIGHTEN_P = 0.45              # dynamic TP: p below this …
TP_TIGHTEN_ELAPSED_FRAC = 0.5    # … past 50% of the holding window
TP_TIGHTEN_KEEP = 0.5            # new target keeps 50% of remaining distance
TP_TIGHTEN_MAX_PROGRESS = 0.8    # no TP surgery when nearly there
TRAIL_START_R = 0.5              # volatility trailing arms at +0.5R
TRAIL_VOL_MULT = 1.5             # trail distance = 1.5 × 1-min volatility
LIQ_SPREAD_PCTL = 0.9            # liquidity deterioration threshold
LIQ_LOCK_MIN_R = 0.3             # liquidity lock only once decently green


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


def hold_ev_r(p_target: float, dist_target_pips: float,
              dist_stop_pips: float) -> float:
    """Phase B — continuous expected value of HOLDING, in R of the remaining
    stop distance. With a pure driftless barrier probability this is ~0 by
    construction; it goes negative exactly when the drift tilt, time decay
    or excursion penalties say the remaining edge is gone."""
    if dist_stop_pips <= 0:
        return 0.0
    return (p_target * dist_target_pips
            - (1.0 - p_target) * dist_stop_pips) / dist_stop_pips


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
             spread_limit: float | None = None,
             vol_short_pips: float | None = None,
             spread_pctl: float | None = None,
             partial_done: bool = False) -> dict:
    """Deterministic per-second decision for one open scalp.

    p_target MUST be the position-conditioned probability from
    position_p_target() — never a fresh-entry model score.
    target_px is the CURRENT (possibly adaptively tightened) target."""
    sign = 1.0 if direction == "BUY" else -1.0
    target_dist = abs(target_px - entry_px) or pip_size
    stop_dist = abs(entry_px - stop_px) or pip_size
    progress = sign * (mid - entry_px) / target_dist   # 1.0 = at target
    profit_r = sign * (mid - entry_px) / stop_dist     # gain in R of stop
    in_profit = progress > 0
    dist_t = max(sign * (target_px - mid) / pip_size, 0.0)
    dist_s = max(sign * (mid - stop_px) / pip_size, 0.0)

    # ---------------- EXIT_NOW (highest priority) ----------------
    # 0 · virtual TP reached — the broker only knows the ORIGINAL target,
    # so an adaptively tightened target is realised by the engine itself.
    if progress >= 1.0:
        return {"action": "EXIT_NOW", "reason": "adaptive_tp_hit",
                "progress": round(progress, 3)}
    # 1 · position probability collapsed and no meaningful progress
    if p_target is not None and p_target < P_EXIT_FLOOR and progress < 0.5:
        return {"action": "EXIT_NOW", "reason": "adaptive_p_collapse",
                "p_target": round(p_target, 3), "progress": round(progress, 3)}
    # 2 · directional regime flipped against the position
    if regime_opposes and progress < 0.25:
        return {"action": "EXIT_NOW", "reason": "adaptive_regime_flip",
                "progress": round(progress, 3)}
    # 3 · Phase B — continuous EV: holding has negative expectancy while the
    # position is green → lock the gain instead of donating it back
    if p_target is not None and profit_r >= EV_MIN_PROFIT_R:
        ev_r = hold_ev_r(p_target, dist_t, dist_s)
        if ev_r < EV_EXIT_R:
            return {"action": "EXIT_NOW", "reason": "adaptive_ev_negative",
                    "ev_r": round(ev_r, 3), "p_target": round(p_target, 3),
                    "progress": round(progress, 3)}

    # ---------------- PARTIAL_CLOSE (Phase B) ----------------
    # bank half at a solid gain when the remaining edge has weakened
    if (not partial_done and profit_r >= PARTIAL_TRIGGER_R
            and ((p_target is not None and p_target < PARTIAL_P_MAX)
                 or (vol_ratio is not None and vol_ratio >= 2.0))):
        return {"action": "PARTIAL_CLOSE", "fraction": PARTIAL_FRACTION,
                "reason": "adaptive_partial_lock",
                "profit_r": round(profit_r, 3),
                "progress": round(progress, 3)}

    # ---------------- TIGHTEN_STOP proposals ----------------
    # (envelope-clamped by the caller via clamp_tighter)
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
    elif (spread_pctl is not None and spread_pctl >= LIQ_SPREAD_PCTL
          and profit_r >= LIQ_LOCK_MIN_R):
        # Phase B — liquidity-aware: exit costs are deteriorating; lock
        # breakeven while the gain still covers the widened spread
        reason = "adaptive_liquidity_lock"
        proposed = entry_px
    elif (vol_short_pips is not None and profit_r >= TRAIL_START_R):
        # Phase B — volatility-aware trailing: follow at k× short-term vol
        reason = "adaptive_vol_trail"
        proposed = mid - sign * TRAIL_VOL_MULT * vol_short_pips * pip_size
    if proposed is not None:
        return {"action": "TIGHTEN_STOP", "reason": reason,
                "proposed_stop_px": round(proposed, 6),
                "progress": round(progress, 3)}

    # ---------------- TIGHTEN_TP (Phase B, dynamic target) ----------------
    # weak remaining edge late in the trade → bring the target closer so a
    # smaller favorable move still converts to a realised win. Only ever
    # CLOSER — never beyond the original TP.
    if (p_target is not None and p_target < TP_TIGHTEN_P
            and max_holding_ms
            and elapsed_ms >= TP_TIGHTEN_ELAPSED_FRAC * max_holding_ms
            and 0 < progress < TP_TIGHTEN_MAX_PROGRESS):
        new_target = mid + sign * TP_TIGHTEN_KEEP * (target_px - mid)
        return {"action": "TIGHTEN_TP", "reason": "adaptive_tp_tighten",
                "proposed_target_px": round(new_target, 6),
                "p_target": round(p_target, 3),
                "progress": round(progress, 3)}

    return {"action": "HOLD", "reason": None, "progress": round(progress, 3)}
