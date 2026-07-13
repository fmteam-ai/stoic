"""iter-57 · Payoff repair guards.

Two pure guards born from the 2026-07-06 post-mortem (78.9% win rate,
NEGATIVE profit):

1. payoff_guard_block — profit-taking overlays (win_rate Smart Cap) can clip
   TP1 while leaving the SL untouched, inverting the realized R:R
   (risk 17 pts to make 4). Skip any trade whose SL distance exceeds
   `payoff_guard_max_sl_tp1` × the TP1 distance at entry (default 2×).

2. intraday_counter_momentum — the MTF tiers are computed from DAILY bars
   and cannot see today's session. 147/147 trades were SELLs while gold
   rallied +0.8% intraday. Veto trades that fight a strong intraday move
   vs yesterday's close.
"""
from datetime import datetime, timezone

INTRADAY_COUNTER_MOMENTUM_PCT = 0.4          # gold / indices
INTRADAY_COUNTER_MOMENTUM_PCT_CRYPTO = 1.0   # BTC is noisier

# iter-59 · SHORT-tier (last ~week) momentum strength above which fading it
# is forbidden. Shadow-tested 14d: blocks 226 fades netting -$1,044
# (14d total -$439 → +$604; 2026-07-07 session -$815 → +$35). Weaker
# counter-SHORT fades stay allowed — they were net profitable.
SHORT_TIER_FADE_MAX_SLOPE_PCT = 1.0


def short_tier_momentum_veto(action: str, mtf_tiers: dict,
                             threshold: float = SHORT_TIER_FADE_MAX_SLOPE_PCT) -> str | None:
    """Veto trades fading a STRONGLY directional SHORT tier (V-recovery
    failure mode: MEDIUM/LONG SMAs stay bearish for weeks after a reversal,
    so the 2-of-3 MTF vote keeps approving SELLs into a fresh rally)."""
    if action not in ("BUY", "SELL"):
        return None
    sh = (mtf_tiers or {}).get("SHORT") or {}
    slope = sh.get("sma_fast_slope_pct")
    if slope is None:
        return None
    slope = float(slope)
    if action == "SELL" and slope >= threshold:
        return (f"SHORT-tier momentum veto: last-week trend is strongly UP "
                f"(fast-SMA slope {slope:+.2f}% ≥ {threshold}%) — SELL would fade "
                f"a fresh rally the slower tiers haven't caught up to. Vetoed.")
    if action == "BUY" and slope <= -threshold:
        return (f"SHORT-tier momentum veto: last-week trend is strongly DOWN "
                f"(fast-SMA slope {slope:+.2f}% ≤ -{threshold}%) — BUY would fade "
                f"a fresh selloff the slower tiers haven't caught up to. Vetoed.")
    return None


def payoff_guard_apply(signal: dict, cfg: dict) -> dict | None:
    """Evaluate the SL/TP1 payoff ratio of a signal.

    Returns None when the geometry is fine, otherwise:
      {"skip": reason}                      — cfg payoff_guard_mode == "skip"
      {"tighten": new_sl, "sl_pips_scale": f, "reason": ...}
                                            — default: clamp SL to max_ratio × TP1 dist
    """
    if not cfg.get("payoff_guard_enabled", True):
        return None
    try:
        entry = float(signal.get("entry_price") or 0)
        sl = float(signal.get("stop_loss") or 0)
        tp1 = float(signal.get("tp1") or signal.get("take_profit") or 0)
        max_ratio = float(cfg.get("payoff_guard_max_sl_tp1") or 1.2)
    except (TypeError, ValueError):
        return None
    if not (entry and sl and tp1):
        return None
    sl_d = abs(entry - sl)
    tp1_d = abs(tp1 - entry)
    if tp1_d <= 0 or sl_d <= max_ratio * tp1_d:
        return None
    if str(cfg.get("payoff_guard_mode") or "tighten").lower() == "skip":
        return {"skip": (
            f"Payoff guard: SL distance {sl_d:.1f} is {sl_d / tp1_d:.1f}x the TP1 "
            f"distance {tp1_d:.1f} (max {max_ratio:.1f}x) — realized R:R would be "
            f"inverted (risking {sl_d:.1f} to make {tp1_d:.1f}). Trade skipped.")}
    new_dist = max_ratio * tp1_d
    direction = 1 if sl > entry else -1
    new_sl = round(entry + direction * new_dist, 5)
    return {"tighten": new_sl,
            "sl_pips_scale": new_dist / sl_d,
            "reason": (f"Payoff guard: SL tightened {sl_d:.1f} → {new_dist:.1f} "
                       f"({max_ratio:.1f}x TP1 distance {tp1_d:.1f}) — was risking "
                       f"{sl_d:.1f} to make {tp1_d:.1f}.")}


def final_rr_guard(signal: dict, cfg: dict) -> str | None:
    """LAST-line R:R check on the FINAL geometry, run AFTER every overlay
    (Smart Cap TP clipping, payoff-guard SL tighten, behavior adjustments).

    The signal-time R:R veto (min 2.0) runs on the ORIGINAL geometry, so a
    later TP clip could ship trades risking 3x their target (iter-118 audit:
    median executed R:R 0.31 → 73% win rate but net-negative P&L). Weighted
    R:R = (0.5·TP1 + 0.25·TP2 + 0.25·TP3) / SL must clear `min_final_rr`
    (default 0.75 → breakeven win rate 57%).
    """
    if signal.get("action") not in ("BUY", "SELL"):
        return None
    try:
        floor = float(cfg.get("min_final_rr") or 0.75)
        entry = float(signal.get("entry_price") or 0)
        sl = float(signal.get("stop_loss") or 0)
        tp1 = float(signal.get("tp1") or signal.get("take_profit") or 0)
        tp2 = float(signal.get("tp2") or tp1)
        tp3 = float(signal.get("tp3") or tp2)
    except (TypeError, ValueError):
        return None
    if not (entry and sl and tp1) or floor <= 0:
        return None
    sl_d = abs(entry - sl)
    if sl_d <= 0:
        return None
    weighted_tp = 0.5 * abs(tp1 - entry) + 0.25 * abs(tp2 - entry) + 0.25 * abs(tp3 - entry)
    rr = weighted_tp / sl_d
    if rr >= floor:
        return None
    return (f"Final R:R guard: geometry after profit-taking overlays is "
            f"{rr:.2f} (risking {sl_d:.1f} to make {weighted_tp:.1f}, "
            f"min {floor:.2f}) — negative expected value, trade skipped.")


def intraday_counter_momentum(action: str, symbol: str, current_price: float,
                              history: list, today: str | None = None):
    """Returns (veto_reason | None, intraday_change_pct | None).
    Reference close = last DAILY bar strictly before today (UTC)."""
    if action not in ("BUY", "SELL") or not history or not current_price:
        return None, None
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ref = None
    for bar in reversed(history):
        if str(bar.get("date") or "") < today and bar.get("close"):
            ref = float(bar["close"])
            break
    if not ref or ref <= 0:
        return None, None
    chg = round((float(current_price) - ref) / ref * 100.0, 3)
    thr = (INTRADAY_COUNTER_MOMENTUM_PCT_CRYPTO
           if str(symbol).upper().startswith("BTC")
           else INTRADAY_COUNTER_MOMENTUM_PCT)
    if action == "SELL" and chg >= thr:
        return (f"Intraday momentum is UP {chg:+.2f}% vs yesterday's close — "
                f"SELL fights today's tape (threshold ±{thr}%). Vetoed.", chg)
    if action == "BUY" and chg <= -thr:
        return (f"Intraday momentum is DOWN {chg:+.2f}% vs yesterday's close — "
                f"BUY fights today's tape (threshold ±{thr}%). Vetoed.", chg)
    return None, chg


# ─── iter-129 · Session-aware gates (2026-07-13 deep review) ────────────────
# That session: gold slid 85pts (-2.1%). The bot (a) bought counter-trend
# twice mid-slide (-$110), (b) sold the day low after the move was spent,
# stopped on the bounce (-$244), and (c) clipped 12pt TPs out of an 80pt
# trend. Three pure gates fix each failure. All read the M15 feats dict
# from intraday_features.compute_intraday_features.

SESSION_TREND_MIN_RANGE_PCT = 0.45          # gold / indices
SESSION_TREND_MIN_RANGE_PCT_CRYPTO = 0.9
EXHAUSTION_RANGE_PCT = 1.5
EXHAUSTION_RANGE_PCT_CRYPTO = 3.0
EXHAUSTION_POS_BAND = 15.0                  # % of day range near the extreme
TREND_RIDE_RANGE_PCT = 1.0
TREND_RIDE_RANGE_PCT_CRYPTO = 2.5


def _is_crypto(symbol) -> bool:
    return str(symbol).upper().startswith(("BTC", "ETH"))


def _rng_pos(feats):
    rng = float(feats.get("day_range_pct") or 0)
    pos = feats.get("range_pos_pct")
    return rng, (float(pos) if pos is not None else None)


def session_trend_gate(action: str, feats: dict, symbol: str) -> str | None:
    """Veto counter-trend entries against a clear intraday session trend.

    The old counter-momentum veto compared price to YESTERDAY'S close and was
    blind to intra-session structure (gold peaked at 4077 early, ground lower
    all day, yet stayed near flat vs yesterday — BUYs at 4059 passed)."""
    if action not in ("BUY", "SELL") or not feats:
        return None
    rng, pos = _rng_pos(feats)
    if pos is None:
        return None
    thr = (SESSION_TREND_MIN_RANGE_PCT_CRYPTO if _is_crypto(symbol)
           else SESSION_TREND_MIN_RANGE_PCT)
    if rng < thr:
        return None
    slope = float(feats.get("ema20_slope_pct_2h") or 0)
    structure = feats.get("swing_structure")
    if action == "BUY" and pos <= 40 and (slope <= -0.02 or structure == "LH_LL"):
        return (f"Session-trend gate: {rng:.2f}% range day, price at {pos:.0f}% of "
                f"the session range, EMA20 slope {slope:+.2f}%/2h ({structure}) — "
                f"BUY fights the intraday downtrend. Vetoed.")
    if action == "SELL" and pos >= 60 and (slope >= 0.02 or structure == "HH_HL"):
        return (f"Session-trend gate: {rng:.2f}% range day, price at {pos:.0f}% of "
                f"the session range, EMA20 slope {slope:+.2f}%/2h ({structure}) — "
                f"SELL fights the intraday uptrend. Vetoed.")
    return None


def exhaustion_chase_gate(action: str, feats: dict, symbol: str) -> str | None:
    """Veto WITH-trend entries once the day's move is already spent.

    Mirror of the knife filter: that one blocks FADING extremes, this one
    blocks CHASING them (selling the day low after a -2% slide → stopped on
    the mean-reversion bounce)."""
    if action not in ("BUY", "SELL") or not feats:
        return None
    rng, pos = _rng_pos(feats)
    if pos is None:
        return None
    thr = (EXHAUSTION_RANGE_PCT_CRYPTO if _is_crypto(symbol)
           else EXHAUSTION_RANGE_PCT)
    if rng < thr:
        return None
    if action == "SELL" and pos <= EXHAUSTION_POS_BAND:
        return (f"Exhaustion gate: {rng:.2f}% down-day already elapsed and price "
                f"at {pos:.0f}% of the range (day-low zone) — selling a spent "
                f"move risks the bounce. Vetoed.")
    if action == "BUY" and pos >= 100 - EXHAUSTION_POS_BAND:
        return (f"Exhaustion gate: {rng:.2f}% up-day already elapsed and price "
                f"at {pos:.0f}% of the range (day-high zone) — buying a spent "
                f"move risks the pullback. Vetoed.")
    return None


def trend_ride_check(action: str, feats: dict, symbol: str) -> dict | None:
    """On big directional days, with-trend entries should ride, not clip.

    Returns context dict when the entry is WITH a strong session trend and
    not yet at the exhaustion extreme — caller widens TP and lets the
    breakeven/trailing engine capture the run."""
    if action not in ("BUY", "SELL") or not feats:
        return None
    rng, pos = _rng_pos(feats)
    if pos is None:
        return None
    thr = (TREND_RIDE_RANGE_PCT_CRYPTO if _is_crypto(symbol)
           else TREND_RIDE_RANGE_PCT)
    if rng < thr:
        return None
    slope = float(feats.get("ema20_slope_pct_2h") or 0)
    if action == "SELL" and EXHAUSTION_POS_BAND < pos <= 45 and slope <= 0:
        return {"day_range_pct": rng, "range_pos_pct": pos, "direction": "DOWN"}
    if action == "BUY" and 55 <= pos < 100 - EXHAUSTION_POS_BAND and slope >= 0:
        return {"day_range_pct": rng, "range_pos_pct": pos, "direction": "UP"}
    return None
