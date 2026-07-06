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


def payoff_guard_block(signal: dict, cfg: dict) -> str | None:
    if not cfg.get("payoff_guard_enabled", True):
        return None
    try:
        entry = float(signal.get("entry_price") or 0)
        sl = float(signal.get("stop_loss") or 0)
        tp1 = float(signal.get("tp1") or signal.get("take_profit") or 0)
        max_ratio = float(cfg.get("payoff_guard_max_sl_tp1") or 2.0)
    except (TypeError, ValueError):
        return None
    if not (entry and sl and tp1):
        return None
    sl_d = abs(entry - sl)
    tp1_d = abs(tp1 - entry)
    if tp1_d <= 0 or sl_d <= max_ratio * tp1_d:
        return None
    return (f"Payoff guard: SL distance {sl_d:.1f} is {sl_d / tp1_d:.1f}x the TP1 "
            f"distance {tp1_d:.1f} (max {max_ratio:.1f}x) — realized R:R would be "
            f"inverted (risking {sl_d:.1f} to make {tp1_d:.1f}). Trade skipped.")


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
