"""A+ Confluence Filter — only let through the highest-quality setups.

Run AFTER all other vetoes have decided `final_action`. Counts how many
secondary checks confirm the trade. If fewer than `min_confluences` (default
4 of 6) confirm, the signal is held.

The six checks:
  1. MTF aligned                          (always relevant)
  2. Learned-meta p_win > 0.55            (only counts if model is trained)
  3. COT not overcrowded *against* trade  (XAU only — auto-pass on BTC)
  4. TIPS regime not opposed              (XAU only — auto-pass on BTC)
  5. Session is London (07-11 UTC) or NY-overlap (12-16 UTC)
  6. ATR in 25-75 percentile vs trailing 60-day distribution
     (skip flat-chop AND violent-spike regimes)

For non-XAU symbols, checks 3 and 4 are treated as auto-pass so the score
is comparable across instruments.
"""
from typing import Optional


def _atr_pct_check(history: list, indicators: dict, lookback: int = 60) -> Optional[bool]:
    """Return True iff current ATR sits in the 25-75 percentile vs lookback."""
    if not history or len(history) < lookback + 1:
        return None
    try:
        atrs = []
        for i in range(len(history) - lookback, len(history) - 1):
            if i < 1:
                continue
            prev = history[i - 1]
            cur = history[i]
            if not all(k in cur for k in ("high", "low", "close")) or "close" not in prev:
                continue
            tr = max(
                float(cur["high"]) - float(cur["low"]),
                abs(float(cur["high"]) - float(prev["close"])),
                abs(float(cur["low"]) - float(prev["close"])),
            )
            atrs.append(tr)
        if len(atrs) < 20:
            return None
        cur_atr = indicators.get("atr_14")
        if cur_atr is None:
            cur_atr = atrs[-1] if atrs else None
        if cur_atr is None:
            return None
        atrs_sorted = sorted(atrs)
        n = len(atrs_sorted)
        p25 = atrs_sorted[n // 4]
        p75 = atrs_sorted[(3 * n) // 4]
        return p25 <= cur_atr <= p75
    except Exception:
        return None


def _session_check(session: dict) -> bool:
    """True if London or NY-overlap session is active."""
    if not session:
        return False
    name = (session.get("primary") or session.get("name") or "").lower()
    if any(s in name for s in ("london", "ny_overlap", "ny-overlap", "newyork", "us")):
        return True
    # Fallback by UTC hour if name not present
    h = session.get("utc_hour")
    if isinstance(h, int):
        return 7 <= h < 16
    return False


def confluence_check(
    *,
    action: str,
    symbol: str,
    mtf: dict,
    learned_meta: Optional[dict],
    cot: Optional[dict],
    tips: Optional[dict],
    session: dict,
    history: list,
    indicators: dict,
    min_confluences: int = 4,
) -> dict:
    """Return {passed: bool, score: int, max: 6, checks: {...}, reason: str}."""
    is_xau = (symbol or "").upper() == "XAUUSD"
    is_buy = action == "BUY"

    # 1. MTF aligned
    mtf_ok = bool(mtf.get("aligned", True))

    # 2. Learned meta p_win > 0.55 — counts as pass if model is untrained
    if learned_meta and learned_meta.get("p_win") is not None:
        lm_ok = float(learned_meta["p_win"]) >= 0.55
    else:
        lm_ok = True  # untrained classifier doesn't penalise

    # 3. COT not overcrowded against — auto-pass on non-XAU
    if is_xau and cot:
        if is_buy:
            cot_ok = not bool(cot.get("overcrowded_long"))
        else:
            cot_ok = not bool(cot.get("overcrowded_short"))
    else:
        cot_ok = True

    # 4. TIPS regime not opposed — auto-pass on non-XAU
    if is_xau and tips:
        regime = tips.get("regime")
        if is_buy:
            tips_ok = regime != "bearish_gold"
        else:
            tips_ok = regime != "bullish_gold"
    else:
        tips_ok = True

    # 5. Session quality
    sess_ok = _session_check(session)

    # 6. ATR percentile
    atr_ok = _atr_pct_check(history, indicators)
    if atr_ok is None:
        atr_ok = True   # not enough data → don't penalise

    checks = {
        "mtf_aligned": mtf_ok,
        "learned_meta_pass": lm_ok,
        "cot_not_opposed": cot_ok,
        "tips_not_opposed": tips_ok,
        "good_session": sess_ok,
        "atr_in_range": atr_ok,
    }
    score = sum(1 for v in checks.values() if v)
    passed = score >= min_confluences
    reason = ""
    if not passed and action != "HOLD":
        failed = [k for k, v in checks.items() if not v]
        reason = (
            f"A+ filter: only {score}/6 confluences confirmed "
            f"(need ≥{min_confluences}). Missing: {', '.join(failed)}."
        )
    return {
        "passed": passed,
        "score": score,
        "max": 6,
        "min_required": min_confluences,
        "checks": checks,
        "reason": reason,
    }
