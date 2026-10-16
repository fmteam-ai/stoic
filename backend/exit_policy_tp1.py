"""v1.60.60 #72 — exit policy "TP1 bank + protected runner" for hf_scalp / hf_scalp_fast / breakout_m15 / gold_trend_pulse.

Pure planning functions; trade_manager wires them. Per-account BotConfig `exit_policy` = "fixed_tp" (default) |
"tp1_bank_runner". At entry: SL, TP1 (= the engine's original TP, never moves), TP2 (= entry ± tp2_multiple × TP1
distance). Broker TP is TP1 until the bank, TP2 afterwards. At TP1: close tp1_close_fraction (floored to the lot step),
move the remainder's SL to entry ± be_plus_offset_price (respecting the broker min stop distance), log `tp1_bank`.
Runner: trail behind the last M5 swing or 1.0 × ATR(M5) (whichever is tighter), profit direction only.
"""
from __future__ import annotations

from datetime import datetime, timezone

ENGINES = {"hf_scalp", "hf_scalp_fast", "breakout_m15", "gold_trend_pulse"}
POLICIES = ("fixed_tp", "tp1_bank_runner")
DEFAULTS = {"exit_policy": "fixed_tp", "tp1_close_fraction": 0.5, "be_plus_offset_price": 1.0, "tp2_multiple": 2.0}


def policy_for(cfg: dict | None) -> str:
    p = str((cfg or {}).get("exit_policy") or DEFAULTS["exit_policy"])
    return p if p in POLICIES else "fixed_tp"


def applies(trade: dict, cfg: dict | None) -> bool:
    eng = trade.get("engine") or trade.get("scope")
    return policy_for(cfg) == "tp1_bank_runner" and eng in ENGINES and trade.get("status") == "open"


def plan_targets(entry: float, stop: float, tp1: float, action: str, tp2_multiple: float = 2.0) -> dict:
    """TP1 = original engine TP (immutable); TP2 = entry ± tp2_multiple × |TP1 − entry| on the profit side."""
    d = abs(float(tp1) - float(entry))
    tp2 = float(entry) + d * float(tp2_multiple) * (1 if action == "BUY" else -1)
    return {"sl": float(stop), "tp1": float(tp1), "tp2": round(tp2, 5), "tp1_distance": round(d, 5), "action": action}


def floor_lot(lots: float, lot_step: float = 0.01, min_lot: float = 0.01) -> float:
    step = float(lot_step or 0.01)
    n = int(round(float(lots) / step + 1e-9))
    v = round(n * step, 8)
    while v > float(lots) + 1e-9 and n > 0:          # floor, never round up
        n -= 1
        v = round(n * step, 8)
    return v if v >= float(min_lot) - 1e-9 else 0.0


def tp1_reached(action: str, price: float, tp1: float, buffer: float = 0.0) -> bool:
    return (price >= tp1 - buffer) if action == "BUY" else (price <= tp1 + buffer)


def protected_sl(entry: float, action: str, offset_price: float = 1.0) -> float:
    return round(float(entry) + (float(offset_price) if action == "BUY" else -float(offset_price)), 5)


def respects_min_stop(action: str, new_sl: float, price: float, min_stop_distance_px: float | None) -> bool:
    d = float(min_stop_distance_px or 0)
    return (price - new_sl >= d) if action == "BUY" else (new_sl - price >= d)


def bank_plan(trade: dict, price: float, *, lot_step: float = 0.01, min_lot: float = 0.01, min_stop_distance_px: float | None = None,
              close_fraction: float = 0.5, offset_price: float = 1.0, tp2: float | None = None) -> dict:
    """What to send when TP1 is hit: {close_volume, keep_volume, new_sl, new_tp, sl_only, note}."""
    action = trade["action"]
    entry = float(trade["entry_price"])
    lot = float(trade.get("current_lot") or trade.get("lot_size") or 0)
    close_vol = floor_lot(lot * float(close_fraction), lot_step, min_lot)
    keep = round(lot - close_vol, 8)
    if close_vol <= 0 or keep < float(min_lot) - 1e-9:
        close_vol, keep, sl_only = 0.0, lot, True                     # cannot split → SL move only
    else:
        sl_only = False
    new_sl = protected_sl(entry, action, offset_price)
    if not respects_min_stop(action, new_sl, price, min_stop_distance_px):
        d = float(min_stop_distance_px or 0)
        new_sl = round(price - d if action == "BUY" else price + d, 5)   # clamp to the broker minimum, still ≥ entry+offset? keep tighter of the two
        new_sl = min(new_sl, protected_sl(entry, action, offset_price)) if action == "BUY" else max(new_sl, protected_sl(entry, action, offset_price))
    cur_sl = trade.get("stop_loss")
    if cur_sl is not None and not tighter(action, new_sl, float(cur_sl)):
        new_sl = float(cur_sl)                                        # never loosen
    return {"close_volume": close_vol, "keep_volume": keep, "new_sl": new_sl, "new_tp": tp2, "sl_only": sl_only,
            "note": "tp1_bank: SL-only (volume cannot be split)" if sl_only else f"tp1_bank: close {close_vol} keep {keep}"}


def tighter(action: str, new_sl: float, current_sl: float) -> bool:
    return new_sl > current_sl if action == "BUY" else new_sl < current_sl


def norm_bars(bars: list) -> list[dict]:
    """Accept {high,low,close} or the stored {h,l,c} shape."""
    out = []
    for b in bars or []:
        if not isinstance(b, dict):
            continue
        h, lo, c = b.get("high", b.get("h")), b.get("low", b.get("l")), b.get("close", b.get("c"))
        if h is None or lo is None or c is None:
            continue
        out.append({"high": float(h), "low": float(lo), "close": float(c)})
    return out


def swing_level(m5_bars: list[dict], action: str, lookback: int = 6) -> float | None:
    """Last M5 swing low (BUY) / high (SELL) over completed bars."""
    bars = norm_bars(m5_bars)[-lookback:]
    if len(bars) < 2:
        return None
    return min(float(b["low"]) for b in bars) if action == "BUY" else max(float(b["high"]) for b in bars)


def atr(m5_bars: list[dict], n: int = 14) -> float | None:
    bars = norm_bars(m5_bars)
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-n - 1:-1], bars[-n:]):
        h, lo, pc = float(cur["high"]), float(cur["low"]), float(prev["close"])
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return sum(trs) / len(trs)


def trail_sl(action: str, current_sl: float, price: float, m5_bars: list[dict], *, atr_mult: float = 1.0,
             min_stop_distance_px: float | None = None) -> float | None:
    """Runner trail: tighter of (last M5 swing, price ∓ 1.0×ATR). Returns a new SL only if it tightens and respects the min stop."""
    swing, a = swing_level(m5_bars, action), atr(m5_bars)
    cands = []
    if swing is not None:
        cands.append(swing)
    if a is not None:
        cands.append(price - a * atr_mult if action == "BUY" else price + a * atr_mult)
    if not cands:
        return None
    new_sl = round(max(cands) if action == "BUY" else min(cands), 5)   # tighter = closer to price on the profit side
    if not tighter(action, new_sl, float(current_sl)):
        return None
    if not respects_min_stop(action, new_sl, price, min_stop_distance_px):
        return None
    return new_sl


def split_pnl(trade: dict) -> dict:
    """Banked (TP1 partial) vs runner P&L for the trade report. Uses recorded partial deals when present, else estimates."""
    bank = trade.get("tp1_bank") or {}
    if not bank:
        return {"banked_pnl": 0.0, "runner_pnl": round(float(trade.get("pnl") or 0), 2), "policy": "fixed_tp"}
    banked = bank.get("pnl")
    if banked is None:
        try:
            entry, tp1, vol = float(trade["entry_price"]), float(bank["price"]), float(bank.get("close_volume") or 0)
            pv = float(trade.get("pip_value_usd_per_lot") or 1.0)
            pip = float(trade.get("pip_size") or 0.1)
            banked = round(((tp1 - entry) if trade["action"] == "BUY" else (entry - tp1)) / pip * pv * vol, 2)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            banked = 0.0
    total = float(trade.get("pnl") or 0)
    return {"banked_pnl": round(float(banked), 2), "runner_pnl": round(total - float(banked), 2), "policy": "tp1_bank_runner",
            "tp1_at": bank.get("at")}


def bank_record(plan: dict, price: float, attempt: int) -> dict:
    return {"at": datetime.now(timezone.utc).isoformat(), "price": price, "close_volume": plan["close_volume"],
            "keep_volume": plan["keep_volume"], "new_sl": plan["new_sl"], "new_tp": plan.get("new_tp"), "sl_only": plan["sl_only"],
            "attempt": attempt, "pnl": None}
