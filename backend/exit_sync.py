"""v1.60.36 — keep a trade's SL/TP geometry consistent across widening, rescaling and the real fill.

* `widen_signal_targets(signal, mult, symbol)` — trend-ride: widen take_profit AND tp1/tp2/tp3/tp_pips together
  (capped per tier) so the broker TP and the trade's tp fields never disagree (14 Oct: tp3 4077.12 vs broker 4071.01).
* `tp_fields_from_pips(entry, action, symbol, tp_pips)` — tp1/tp2/tp3 prices for a (rescaled) pip ladder.
* `fill_rebase(trade, fill)` — when the fill is > REBASE_MIN_PIPS from the signal price, re-base the stop to the fill
  keeping the designed sl_pips (fill-to-SL distance = sizing distance). Only ever tightens (a worse fill moves the
  stop closer to price); a favourable fill keeps the original, already-tighter stop.
"""
from __future__ import annotations

from pip_utils import pips_to_price, price_to_pips

REBASE_MIN_PIPS = 2.0


def _tier_caps():
    from ai_signals import MTF_TP1_MAX_PIPS, MTF_TP2_MAX_PIPS, MTF_TP3_MAX_PIPS
    return [MTF_TP1_MAX_PIPS, MTF_TP2_MAX_PIPS, MTF_TP3_MAX_PIPS]


def widen_signal_targets(signal: dict, mult: float, symbol: str) -> bool:
    """Trend-ride widening applied to every target field at once. Returns True when applied."""
    entry = float(signal.get("entry_price") or 0)
    tp = float(signal.get("take_profit") or 0)
    if not entry or not tp or mult <= 1.0:
        return False
    caps = _tier_caps()
    sign = 1 if tp >= entry else -1
    cap3 = pips_to_price(symbol, caps[2])
    signal["take_profit"] = round(entry + sign * min(abs(tp - entry) * mult, cap3), 5)
    for i, key in enumerate(("tp1", "tp2", "tp3")):
        v = signal.get(key)
        if v:
            cap = pips_to_price(symbol, caps[i])
            signal[key] = round(entry + sign * min(abs(float(v) - entry) * mult, cap), 5)
    tps = signal.get("tp_pips")
    if isinstance(tps, (int, float)):
        signal["tp_pips"] = round(min(float(tps) * mult, caps[2]), 1)
    elif isinstance(tps, list) and tps:
        signal["tp_pips"] = [round(min(float(v) * mult, caps[min(i, 2)]), 1) for i, v in enumerate(tps)]
    return True


def tp_fields_from_pips(entry: float, action: str, symbol: str, tp_pips: list) -> dict:
    sign = 1 if action == "BUY" else -1
    out = {}
    for i, key in enumerate(("tp1", "tp2", "tp3")):
        if i < len(tp_pips):
            out[key] = round(float(entry) + sign * pips_to_price(symbol, float(tp_pips[i])), 5)
    return out


def signal_slippage(action: str, signal_price: float, fill: float, symbol: str) -> tuple[float, float]:
    """(adverse-only slippage pips, signed gap pips) measured against the SIGNAL price. Positive gap = worse fill."""
    adverse = (signal_price - fill) if action == "SELL" else (fill - signal_price)
    return round(price_to_pips(symbol, max(0.0, adverse)), 2), round(price_to_pips(symbol, adverse), 2)


def fill_rebase(trade: dict, fill: float) -> dict | None:
    """Stop re-based to the actual fill at the designed sl_pips, or None when not needed / would loosen."""
    from trade_manager import tightened_sl
    symbol = trade.get("symbol") or ""
    signal_price = float(trade.get("entry_price") or 0)
    sl_pips = float(trade.get("sl_pips") or 0)
    if not signal_price or not fill or sl_pips <= 0:
        return None
    gap_pips = price_to_pips(symbol, abs(fill - signal_price))
    if gap_pips <= REBASE_MIN_PIPS:
        return None
    sign = -1 if trade.get("action") == "BUY" else 1
    new_sl = round(fill + sign * pips_to_price(symbol, sl_pips), 5)
    if tightened_sl(trade, new_sl) is None:
        return None
    return {"new_sl": new_sl, "gap_pips": round(gap_pips, 2), "signal_price": signal_price, "fill": fill,
            "sl_pips": sl_pips, "previous_sl": float(trade.get("stop_loss") or 0)}
