"""iter-113 · Self-Evaluation Agent — after every trade: "why was I wrong?"

Each closed trade is graded on entry quality and exit quality (using MFE/MAE
recomputed from M15 candles), tagged with regime / volatility / news
context, and screened against a mistake taxonomy:
  counter_trend_entry · entered_into_opposing_liquidity · traded_into_news ·
  stop_too_tight (stopped then reversed) · left_money_on_table ·
  low_confidence_entry · oversized_in_drawdown
Losses additionally get a Claude one-liner lesson ("why was I wrong").

ADJUSTS FUTURE BEHAVIOR: recurring mistakes become live adjustments —
stop_too_tight → widen SL ×1.2 · left_money_on_table → extend TP ×1.15 ·
low_confidence_entry → raise the confidence floor +5pp — applied to every
new signal in bot_runner."""
import logging
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SWEEP_EVERY_S = 300
EVAL_LOOKBACK_H = 48
MAX_PER_SWEEP = 10
ADJ_WINDOW = 20
ADJ_FRACTION = 0.30

_last_sweep = 0.0


def _epoch(iso):
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def excursions(bars, t0, t1, entry, action):
    """(mfe, mae) in price units from bars within [t0, t1]."""
    win = [b for b in bars or [] if t0 is not None and t1 is not None
           and t0 - 900 <= b["t"] <= t1 + 900]
    if not win or not entry:
        return None, None
    hi = max(b["h"] for b in win)
    lo = min(b["l"] for b in win)
    if action == "BUY":
        return max(0.0, hi - entry), max(0.0, entry - lo)
    return max(0.0, entry - lo), max(0.0, hi - entry)


def detect_mistakes(trade, sig, mfe_r, mae_r, post_reversal) -> list:
    out = []
    action = trade.get("action")
    adir = 1 if action == "BUY" else -1
    tiers = sig.get("mtf_tiers") or {}
    dirs = [1 if (tiers.get(k) or {}).get("direction") == "UP" else
            -1 if (tiers.get(k) or {}).get("direction") == "DOWN" else 0
            for k in ("SHORT", "MEDIUM", "LONG")]
    if sum(1 for d in dirs if d == -adir) >= 2:
        out.append("counter_trend_entry")
    lm = sig.get("liquidity") or {}
    zone = lm.get("active_zone")
    if (action == "BUY" and zone == "SUPPLY") or \
       (action == "SELL" and zone == "DEMAND"):
        out.append("entered_into_opposing_liquidity")
    cal = sig.get("calendar_intel") or {}
    m2e = cal.get("minutes_to")
    if m2e is not None and -15 <= m2e <= 45:
        out.append("traded_into_news_event")
    won = float(trade.get("pnl") or 0) > 0
    if not won and post_reversal:
        out.append("stop_too_tight")
    if won and mfe_r is not None:
        realized_r = _realized_r(trade, sig)
        if realized_r is not None and realized_r > 0 and \
                mfe_r >= 2.0 * realized_r and mfe_r >= 1.0:
            out.append("left_money_on_table")
    unc = sig.get("uncertainty") or {}
    if unc.get("risk") == "HIGH" or \
            (unc.get("confidence_pct") or 100) < 60:
        out.append("low_confidence_entry")
    asz = sig.get("adaptive_sizing") or {}
    if (asz.get("drawdown_frac") or 0) >= 0.05 and \
            (asz.get("multiplier") or 1.0) > 1.0:
        out.append("oversized_in_drawdown")
    return out


def _realized_r(trade, sig):
    entry = trade.get("entry_price") or sig.get("entry_price")
    sl = trade.get("stop_loss") or sig.get("stop_loss")
    exitp = trade.get("close_price") or trade.get("exit_price")
    if not entry or not sl or not exitp:
        return None
    risk = abs(float(entry) - float(sl))
    if risk <= 0:
        return None
    move = float(exitp) - float(entry)
    if trade.get("action") == "SELL":
        move = -move
    return round(move / risk, 2)


def grade_entry(sig) -> int:
    q = 50
    score = (sig.get("consensus") or {}).get("score")
    if score is not None:
        q += 15 if score >= 65 else (-10 if score < 55 else 5)
    risk = (sig.get("uncertainty") or {}).get("risk")
    q += {"LOW": 10, "HIGH": -15}.get(risk, 0)
    lm = sig.get("liquidity") or {}
    if lm.get("draw"):
        aligned = (lm["draw"] == "UP") == (sig.get("action") == "BUY")
        q += 10 if aligned else -10
    ev = (sig.get("monte_carlo") or {}).get("ev_r")
    if ev is not None:
        q += 10 if ev > 0.2 else (-15 if ev < 0 else 0)
    return max(0, min(100, q))


def grade_exit(trade, sig, mfe_r, mae_r, post_reversal) -> int:
    won = float(trade.get("pnl") or 0) > 0
    realized_r = _realized_r(trade, sig)
    if won:
        if mfe_r and realized_r is not None and mfe_r > 0:
            return max(20, min(100, round(100 * max(realized_r, 0) / mfe_r)))
        return 70
    if post_reversal:
        return 30
    if mae_r is not None and mae_r > 1.3:
        return 40    # blew past the stop — slippage/gap
    return 60        # clean stop-out, plan worked as designed


def evaluate_trade(trade, sig, bars) -> dict:
    action = trade.get("action")
    entry = float(trade.get("entry_price") or sig.get("entry_price") or 0)
    sl = float(trade.get("stop_loss") or sig.get("stop_loss") or 0)
    risk = abs(entry - sl) or None
    t0 = _epoch(trade.get("opened_at") or trade.get("created_at"))
    t1 = _epoch(trade.get("closed_at"))
    mfe, mae = excursions(bars, t0, t1, entry, action)
    mfe_r = round(mfe / risk, 2) if mfe is not None and risk else None
    mae_r = round(mae / risk, 2) if mae is not None and risk else None
    post_reversal = False
    won = float(trade.get("pnl") or 0) > 0
    if not won and t1 and bars and risk:
        post = [b for b in bars if t1 < b["t"] <= t1 + 8 * 900]
        if post:
            if action == "BUY":
                post_reversal = max(b["h"] for b in post) >= entry + 0.5 * risk
            else:
                post_reversal = min(b["l"] for b in post) <= entry - 0.5 * risk
    mistakes = detect_mistakes(trade, sig, mfe_r, mae_r, post_reversal)
    vol_state = "unknown"
    if bars and len(bars) >= 30:
        from adaptive_sizing import volatility_mult
        vm = volatility_mult(bars)
        vol_state = ("compressed" if vm > 1.05 else
                     "expanded" if vm < 0.95 else "normal")
    return {"outcome": "win" if won else "loss",
            "pnl": trade.get("pnl"),
            "realized_r": _realized_r(trade, sig),
            "entry_quality": grade_entry(sig),
            "exit_quality": grade_exit(trade, sig, mfe_r, mae_r, post_reversal),
            "mfe_r": mfe_r, "mae_r": mae_r,
            "stopped_then_reversed": post_reversal,
            "regime": sig.get("regime"),
            "volatility": vol_state,
            "news": {"net": (sig.get("news_ai") or {}).get("net"),
                     "label": (sig.get("news_ai") or {}).get("label")},
            "mistakes": mistakes}


async def _lesson_llm(trade, ev) -> str | None:
    """Claude one-liner: why was I wrong? (losses only)"""
    import llm_client
    ctx = (f"Trade: {trade.get('action')} {trade.get('symbol')} closed at "
           f"{ev.get('realized_r')}R. Entry quality {ev['entry_quality']}/100, "
           f"exit {ev['exit_quality']}/100, MFE {ev.get('mfe_r')}R, MAE "
           f"{ev.get('mae_r')}R, stopped-then-reversed="
           f"{ev['stopped_then_reversed']}, regime {ev.get('regime')}, "
           f"volatility {ev['volatility']}, mistakes: "
           f"{', '.join(ev['mistakes']) or 'none flagged'}.")
    res = await llm_client.complete(
        feature="self_evaluation",
        system=("You are a trading coach reviewing a losing trade. Answer "
                "'why was I wrong?' in ONE sentence, ≤25 words, specific and "
                "actionable."),
        user=ctx, max_tokens=200,
        usage_meta={"user_id": trade.get("user_id"), "trade_id": str(trade.get("_id"))})
    if not res.ok:
        # Caller treats an exception as "no lesson" (unchanged failure path).
        raise RuntimeError(res.error)
    return res.text.strip()[:300]


def compute_adjustments(evals: list) -> dict:
    """Recurring mistakes → live behavior adjustments."""
    if len(evals) < 5:
        return {}
    n = len(evals)
    counts = {}
    for e in evals:
        for m in e.get("mistakes") or []:
            counts[m] = counts.get(m, 0) + 1
    adj = {}
    if counts.get("stop_too_tight", 0) / n >= ADJ_FRACTION:
        adj["sl_widen_factor"] = 1.2
    if counts.get("left_money_on_table", 0) / n >= ADJ_FRACTION:
        adj["tp_extend_factor"] = 1.15
    if counts.get("low_confidence_entry", 0) / n >= ADJ_FRACTION:
        adj["min_conf_bump"] = 5
    if adj:
        adj["based_on"] = {"evals": n, "mistake_counts": counts}
    return adj


def apply_adjustments(signal: dict, adj: dict) -> dict:
    """Mutates SL/TP on the candidate signal per learned adjustments."""
    applied = {}
    entry = signal.get("entry_price")
    sl = signal.get("stop_loss")
    d = 1 if signal.get("action") == "BUY" else -1
    f = adj.get("sl_widen_factor")
    if f and entry and sl:
        new_sl = round(entry - d * abs(entry - sl) * f, 5)
        applied["stop_loss"] = {"from": sl, "to": new_sl, "factor": f}
        signal["stop_loss"] = new_sl
    g = adj.get("tp_extend_factor")
    tp_key = "tp1" if signal.get("tp1") else "take_profit"
    tp = signal.get(tp_key)
    if g and entry and tp:
        new_tp = round(entry + d * abs(tp - entry) * g, 5)
        applied[tp_key] = {"from": tp, "to": new_tp, "factor": g}
        signal[tp_key] = new_tp
    if adj.get("min_conf_bump"):
        signal["_conf_floor_bump"] = int(adj["min_conf_bump"])
        applied["min_conf_bump"] = adj["min_conf_bump"]
    return applied


async def get_adjustments(db, user_id: str) -> dict:
    doc = await db.behavior_adjustments.find_one({"user_id": user_id})
    return (doc or {}).get("adjustments") or {}


async def sweep_self_evaluation(db) -> int:
    global _last_sweep
    now = time.time()
    if now - _last_sweep < SWEEP_EVERY_S:
        return 0
    _last_sweep = now
    from bson import ObjectId
    from pip_utils import base_symbol
    since = (datetime.now(timezone.utc)
             - timedelta(hours=EVAL_LOOKBACK_H)).isoformat()
    trades = await db.trades.find(
        {"status": "closed", "pnl": {"$ne": None}, "origin": "auto",
         "closed_at": {"$gte": since},
         "self_evaluated": {"$ne": True}}).limit(MAX_PER_SWEEP).to_list(
        MAX_PER_SWEEP)
    done = 0
    touched_users = set()
    for t in trades:
        try:
            sig = {}
            try:
                sig = await db.signals.find_one(
                    {"_id": ObjectId(t.get("signal_id"))}) or {}
            except Exception:
                pass
            cdoc = await db.intraday_candles.find_one(
                {"user_id": t["user_id"],
                 "symbol": base_symbol(t.get("symbol") or "")}, {"bars": 1})
            ev = evaluate_trade(t, sig, (cdoc or {}).get("bars") or [])
            if ev["outcome"] == "loss":
                try:
                    ev["lesson"] = await _lesson_llm(t, ev)
                except Exception as e:  # noqa: BLE001
                    logger.debug("self-eval lesson LLM skipped: %s", e)
            await db.trade_evaluations.insert_one(
                {"trade_id": str(t["_id"]), "user_id": t["user_id"],
                 "symbol": t.get("symbol"), "action": t.get("action"),
                 **ev, "evaluated_at": datetime.now(timezone.utc).isoformat()})
            await db.trades.update_one({"_id": t["_id"]},
                                       {"$set": {"self_evaluated": True}})
            touched_users.add(t["user_id"])
            done += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("self-eval trade %s failed: %s", t.get("_id"), e)
    for uid in touched_users:
        evals = await db.trade_evaluations.find(
            {"user_id": uid}).sort("evaluated_at", -1).limit(
            ADJ_WINDOW).to_list(ADJ_WINDOW)
        adj = compute_adjustments(evals)
        await db.behavior_adjustments.update_one(
            {"user_id": uid},
            {"$set": {"adjustments": adj,
                      "updated_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True)
    if done:
        logger.info("Self-evaluation graded %d trade(s)", done)
    return done
