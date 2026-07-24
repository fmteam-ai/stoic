"""Adaptive exit engine (Phase 2) — exits adjust to LIVE conditions.

Runs inside the trade-manager tick for open non-scalp trades AFTER the
tiered TP logic found nothing to do. Three adjustments, one per tick,
all strictly protective (SL only ever tightens; size only ever shrinks):

  1. VOLATILITY RE-TARGET — ATR expanded/contracted ≥30% since entry →
     scale the remaining TP ladder once (server-side tiers, no EA command).
  2. MOMENTUM-FADE TIGHTEN — in profit ≥0.5R but M15 momentum has turned
     against the trade → lock 40% of the open move via MODIFY_SL.
  3. RESISTANCE DE-RISK — price within 0.3×ATR of opposing session/Donchian
     structure → bank 25% of the remaining size via PARTIAL_CLOSE.
"""
import logging
from datetime import datetime, timedelta, timezone

from pip_utils import base_symbol, pips_to_price, price_to_pips

logger = logging.getLogger("adaptive-exits")

TIGHTEN_COOLDOWN_MIN = 10
VOL_EXPAND = 1.3
VOL_CONTRACT = 0.7
FADE_LOCK_FRAC = 0.4
DERISK_ATR_FRAC = 0.3


def _now():
    return datetime.now(timezone.utc)


def _atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        b, prev = bars[i], bars[i - 1]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - prev["c"]),
                       abs(b["l"] - prev["c"])))
    return sum(trs) / len(trs)


def compute_features(bars: list) -> dict | None:
    """M15 features the exit decisions need. None when data is unusable."""
    if not bars or len(bars) < 24:
        return None
    atr = _atr(bars)
    if not atr or atr <= 0:
        return None
    closes = [b["c"] for b in bars]
    ema = closes[-21]
    k = 2 / 21.0
    for c in closes[-20:]:
        ema = c * k + ema * (1 - k)
    ema_prev = closes[-29] if len(closes) >= 29 else closes[0]
    for c in closes[-28:-8] if len(closes) >= 29 else closes[:-8]:
        ema_prev = c * k + ema_prev * (1 - k)
    last3 = closes[-3:]
    chg4 = closes[-1] - closes[-4]
    day = _now().date()
    today = [b for b in bars
             if datetime.fromtimestamp(float(b["t"]), tz=timezone.utc).date() == day]
    sess_hi = max((b["h"] for b in today), default=max(b["h"] for b in bars[-32:]))
    sess_lo = min((b["l"] for b in today), default=min(b["l"] for b in bars[-32:]))
    don = bars[-21:-1]
    return {
        "atr15": atr,
        "ema20": ema,
        "ema20_slope": ema - ema_prev,
        "chg4": chg4,
        "last3_down": sum(1 for i in (1, 2) if last3[i] < last3[i - 1]),
        "last3_up": sum(1 for i in (1, 2) if last3[i] > last3[i - 1]),
        "session_high": sess_hi, "session_low": sess_lo,
        "donchian_high": max(b["h"] for b in don),
        "donchian_low": min(b["l"] for b in don),
    }


def momentum_fading(action: str, feats: dict) -> bool:
    """≥2 of the last 3 bars against the trade AND the 4-bar net move (or the
    EMA slope) has turned against it — momentum genuinely stalling, not one
    noisy pullback bar inside a healthy trend."""
    if action == "BUY":
        return feats["last3_down"] >= 2 and (feats["ema20_slope"] <= 0
                                             or feats["chg4"] < 0)
    return feats["last3_up"] >= 2 and (feats["ema20_slope"] >= 0
                                       or feats["chg4"] > 0)


def vol_retarget(trade: dict, feats: dict, pips_up: float) -> dict | None:
    """Once per trade, before TP1: scale the TP ladder to current ATR."""
    if trade.get("exit_vol_retarget") or trade.get("tp1_closed"):
        return None
    sym = trade["symbol"]
    sl_pips = float(trade.get("sl_pips") or 0)
    if sl_pips <= 0:
        return None
    entry_atr = trade.get("atr15_at_entry")
    if not entry_atr:
        entry_atr = pips_to_price(sym, sl_pips) / 1.5  # engine geometry: SL≈1.5×ATR
    ratio = feats["atr15"] / float(entry_atr)
    if VOL_CONTRACT < ratio < VOL_EXPAND:
        return None
    scale = min(ratio, 1.6) if ratio >= VOL_EXPAND else max(ratio, 0.65)
    tp_pips = trade.get("tp_pips") or []
    if not tp_pips:
        return None
    new_tp = [round(float(p) * scale, 1) for p in tp_pips]
    if pips_up >= new_tp[0]:
        return None  # never retarget below where price already is
    return {"kind": "EXIT_RETARGET_VOL", "new_tp_pips": new_tp,
            "atr_ratio": round(ratio, 2), "scale": round(scale, 2)}


def fade_tighten(trade: dict, feats: dict, current: float,
                 pips_up: float) -> dict | None:
    sym = trade["symbol"]
    action = trade["action"]
    entry = float(trade["entry_price"])
    sl_pips = float(trade.get("sl_pips") or 0)
    if sl_pips <= 0 or pips_up < 0.5 * sl_pips:
        return None
    if not momentum_fading(action, feats):
        return None
    last = trade.get("exit_last_tighten_at")
    if last:
        try:
            ts = datetime.fromisoformat(str(last))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if _now() - ts < timedelta(minutes=TIGHTEN_COOLDOWN_MIN):
                return None
        except Exception:
            pass
    move = current - entry if action == "BUY" else entry - current
    lock = entry + FADE_LOCK_FRAC * move if action == "BUY" \
        else entry - FADE_LOCK_FRAC * move
    cur_sl = float(trade.get("stop_loss") or 0)
    buffer = 0.5 * feats["atr15"]
    if action == "BUY":
        if lock >= current - buffer:
            return None
        if cur_sl and lock <= cur_sl:
            return None  # only ever tighten
    else:
        if lock <= current + buffer:
            return None
        if cur_sl and lock >= cur_sl:
            return None
    return {"kind": "EXIT_TIGHTEN_FADE", "new_sl": round(lock, 5),
            "locked_pips": round(price_to_pips(sym, abs(lock - entry)), 1)}


def resistance_derisk(trade: dict, feats: dict, current: float,
                      pips_up: float) -> dict | None:
    if trade.get("exit_derisked") or pips_up <= 0:
        return None
    lot = float(trade.get("lot_size") or 0)
    new_lot = round(lot * 0.75, 2)
    if new_lot < 0.01 or new_lot >= lot:
        return None
    action = trade["action"]
    if action == "BUY":
        barrier = min(feats["session_high"], feats["donchian_high"])
        dist = barrier - current
    else:
        barrier = max(feats["session_low"], feats["donchian_low"])
        dist = current - barrier
    if dist < 0 or dist > DERISK_ATR_FRAC * feats["atr15"]:
        return None
    return {"kind": "EXIT_DERISK_RESISTANCE", "new_volume": new_lot,
            "barrier": round(barrier, 5),
            "dist_pips": round(price_to_pips(trade["symbol"], dist), 1)}


async def manage_exits(db, trade: dict, cfg: dict, current: float,
                       pips_up: float) -> bool:
    """One adaptive-exit action max per tick. Returns True when acted."""
    if not cfg.get("adaptive_exits_enabled", True):
        return False
    cdoc = await db.intraday_candles.find_one(
        {"user_id": trade["user_id"], "symbol": base_symbol(trade["symbol"])},
        {"bars": 1, "updated_at": 1})
    bars = (cdoc or {}).get("bars") or []
    if bars:
        import time as _t
        if _t.time() - float(bars[-1].get("t") or 0) > 1800:
            bars = []
    feats = compute_features(bars) if bars else None
    if not feats:
        return False

    now_iso = _now().isoformat()
    from ws_manager import manager as ws_manager
    from trade_events import append, build

    async def _event(etype, detail, data=None):
        try:
            await append(db, build(etype, user_id=trade["user_id"],
                                   trade_id=str(trade["_id"]),
                                   account_id=trade.get("account_id"),
                                   symbol=trade.get("symbol"),
                                   source="adaptive_exits",
                                   payload={"detail": detail, **(data or {})}))
        except Exception:  # noqa: BLE001
            pass

    act = vol_retarget(trade, feats, pips_up)
    if act:
        await db.trades.update_one(
            {"_id": trade["_id"]},
            {"$set": {"tp_pips": act["new_tp_pips"],
                      "exit_vol_retarget": {"atr_ratio": act["atr_ratio"],
                                            "scale": act["scale"],
                                            "at": now_iso}}})
        await ws_manager.broadcast(trade["user_id"], "trade_management", {
            "trade_id": str(trade["_id"]), "action": act["kind"],
            "new_tp_pips": act["new_tp_pips"], "atr_ratio": act["atr_ratio"]})
        await _event("TargetsRescaled",
                     f"TP ladder rescaled ×{act['scale']} — ATR "
                     f"{act['atr_ratio']}× vs entry",
                     {"new_tp_pips": act["new_tp_pips"]})
        logger.info("vol retarget trade=%s scale=%.2f", trade["_id"], act["scale"])
        return True

    act = fade_tighten(trade, feats, current, pips_up)
    if act:
        await db.trades.update_one(
            {"_id": trade["_id"]},
            {"$set": {"pending_modification": {
                          "type": "MODIFY_SL", "new_sl": act["new_sl"],
                          "requested_at": now_iso},
                      "exit_last_tighten_at": now_iso,
                      "exit_fade_tightened": True}})
        await ws_manager.broadcast(trade["user_id"], "trade_management", {
            "trade_id": str(trade["_id"]), "action": act["kind"],
            "new_sl": act["new_sl"], "locked_pips": act["locked_pips"],
            "pips": round(pips_up, 1)})
        await _event("StopTightened",
                     f"stop tightened to {act['new_sl']} — momentum fading "
                     f"with {act['locked_pips']} pips locked",
                     {"new_sl": act["new_sl"]})
        logger.info("fade tighten trade=%s sl→%.5f", trade["_id"], act["new_sl"])
        return True

    act = resistance_derisk(trade, feats, current, pips_up)
    if act:
        await db.trades.update_one(
            {"_id": trade["_id"]},
            {"$set": {"pending_modification": {
                          "type": "PARTIAL_CLOSE",
                          "new_volume": act["new_volume"],
                          "requested_at": now_iso},
                      "exit_derisked": {"barrier": act["barrier"],
                                        "at": now_iso}}})
        await ws_manager.broadcast(trade["user_id"], "trade_management", {
            "trade_id": str(trade["_id"]), "action": act["kind"],
            "to_lot": act["new_volume"], "barrier": act["barrier"],
            "pips": round(pips_up, 1)})
        await _event("PartialCloseRequested",
                     f"25% de-risked into opposing structure at "
                     f"{act['barrier']} ({act['dist_pips']} pips away)",
                     {"new_volume": act["new_volume"]})
        logger.info("resistance de-risk trade=%s lot→%.2f", trade["_id"],
                    act["new_volume"])
        return True
    return False
