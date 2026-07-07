"""iter-60 · Market Posture — plain-language snapshot of every agent's view.

GET /api/bot/posture — aggregates Market Structure, Macro, News, Quant and
Risk agent states + what must happen before the bot trades again."""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db
from pip_utils import base_symbol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bot", tags=["posture"])


def _tier_line(tiers: dict) -> dict:
    out = {}
    for name in ("SHORT", "MEDIUM", "LONG"):
        t = (tiers or {}).get(name) or {}
        out[name] = {"direction": t.get("direction"),
                     "slope_pct": t.get("sma_fast_slope_pct"),
                     "rsi": t.get("rsi")}
    return out


def _unlock_hints(sig: dict) -> list:
    hints = []
    tiers = sig.get("mtf_tiers") or {}
    sh = tiers.get("SHORT") or {}
    md = tiers.get("MEDIUM") or {}
    slope = sh.get("sma_fast_slope_pct")
    if sig.get("short_tier_momentum_veto") and slope is not None:
        d = "SELLs" if slope > 0 else "BUYs"
        hints.append(f"{d} paused: last-week trend is strongly "
                     f"{'UP' if slope > 0 else 'DOWN'} ({slope:+.2f}%). "
                     f"They resume when the weekly slope cools below ±1.0%.")
    if md.get("direction") == "DOWN" and (slope or 0) > 0:
        hints.append(f"BUYs unlock when the monthly tier flips UP "
                     f"(currently {md.get('sma_fast_slope_pct', '?')}% slope) — "
                     f"2-of-3 timeframe confirmation required.")
    if md.get("direction") == "UP" and (slope or 0) < 0:
        hints.append(f"SELLs unlock when the monthly tier flips DOWN "
                     f"(currently {md.get('sma_fast_slope_pct', '?')}% slope).")
    im = sig.get("intraday_momentum") or {}
    if im.get("veto"):
        hints.append(f"Intraday tape is moving {im.get('change_pct', 0):+.2f}% "
                     f"vs yesterday — counter-trend entries paused until it settles.")
    return hints


@router.get("/posture")
async def market_posture(user=Depends(get_current_user)):
    db = get_db()
    uid = str(user.get("id") or user.get("_id"))
    now = datetime.now(timezone.utc)

    symbols = {}
    fed = None
    try:
        from fed_tone import get_fed_tone
        fed = await get_fed_tone()
    except Exception as e:
        logger.warning("posture fed tone failed: %s", e)
    cfgs = await db.bot_configs.find({"user_id": uid, "active": True}).to_list(20)
    traded = sorted({base_symbol(s) for c in cfgs for s in (c.get("symbols") or ["XAUUSD"])}) or ["XAUUSD"]

    for base in traded[:4]:
        sig = await db.signals.find_one(
            {"symbol": {"$regex": f"^{base}"}}, sort=[("_id", -1)]) or {}
        vetoes = [ln.strip() for ln in (sig.get("reasoning") or "").split("\n")
                  if ln.strip().startswith("VETO")]
        cdoc = await db.intraday_candles.find_one({"user_id": uid, "symbol": base})
        fc = None
        try:
            from forecast_agent import get_forecast
            fc = await get_forecast(db, uid, base)
        except Exception as e:
            logger.debug("posture forecast failed: %s", e)
        struct = None
        try:
            from market_structure import structure_snapshot
            struct = structure_snapshot((cdoc or {}).get("bars") or [])
            if not struct.get("ready") and not (cdoc or {}).get("bars"):
                struct = {"ready": False,
                          "reason": "no M15 candles yet — EA v1.42 required",
                          "bars_n": 0}
        except Exception as e:
            logger.debug("posture structure failed: %s", e)
        lmap = None
        try:
            from liquidity_map import build_liquidity_map
            dom_doc = await db.dom_snapshots.find_one(
                {"user_id": uid, "symbol": base})
            lmap = build_liquidity_map((cdoc or {}).get("bars") or [],
                                       dom_doc=dom_doc)
        except Exception as e:
            logger.debug("posture liquidity failed: %s", e)
        cons = None
        bayes_out = None
        try:
            from consensus import compute_consensus
            from rl_policy import get_policy, rl_decision
            from bayes_decision import get_model as _get_bayes, bayes_decision
            pol = await get_policy(db, uid)
            bmodel = await _get_bayes(db, uid)
            ctx = {"mtf_tiers": sig.get("mtf_tiers"), "market_structure": struct,
                   "liquidity": lmap,
                   "forecast": fc, "fed_tone": fed if base == "XAUUSD" else None,
                   "confidence": sig.get("confidence"),
                   "intraday_momentum": sig.get("intraday_momentum"),
                   "session": sig.get("session"), "regime": sig.get("regime")}
            cons = {}
            bayes_out = {}
            for a in ("BUY", "SELL"):
                s2 = {**ctx, "action": a}
                s2["rl_policy"] = rl_decision(pol, s2, base)
                s2["bayes"] = bayes_decision(bmodel, s2, base)
                bayes_out[a] = s2["bayes"]
                cons[a] = compute_consensus(s2)
        except Exception as e:
            logger.debug("posture consensus failed: %s", e)
        symbols[base] = {
            "action": sig.get("action"),
            "confidence": sig.get("confidence"),
            "regime": (sig.get("regime") or {}).get("regime")
                      if isinstance(sig.get("regime"), dict) else sig.get("regime"),
            "tiers": _tier_line(sig.get("mtf_tiers")),
            "intraday_momentum": sig.get("intraday_momentum"),
            "structure": struct or sig.get("market_structure")
                         or {"ready": False, "reason": "no data", "bars_n": 0},
            "range_forecast": sig.get("range_forecast"),
            "liquidity": lmap,
            "forecast": fc,
            "consensus": cons,
            "bayes": bayes_out,
            "active_vetoes": vetoes[-4:],
            "unlock_hints": _unlock_hints(sig),
            "signal_at": str(sig.get("created_at") or ""),
        }

    # Macro agent
    macro = None
    try:
        from macro_feeds import get_macro_snapshot
        macro = await get_macro_snapshot()
    except Exception as e:
        logger.warning("posture macro failed: %s", e)

    # Risk agent state
    rl = None
    try:
        rl_doc = await db.rl_policies.find_one({"user_id": uid})
        if rl_doc:
            min_v = (rl_doc.get("params") or {}).get("min_visits", 8)
            neg = [k for k, v in (rl_doc.get("states") or {}).items()
                   if v["n"] >= min_v and v["mean"] < 0]
            rl = {"trained_at": rl_doc.get("trained_at"),
                  "trades_used": rl_doc.get("trades_used"),
                  "states_learned": len(rl_doc.get("states") or {}),
                  "negative_states": len(neg)}
    except Exception as e:
        logger.warning("posture rl failed: %s", e)

    guards = await db.auto_guards.find(
        {"user_id": uid, "active": True}).to_list(10)
    cutoff = (now - timedelta(minutes=30)).isoformat()
    recent_losses = await db.trades.count_documents(
        {"user_id": uid, "status": "closed", "pnl": {"$lt": 0},
         "closed_at": {"$gte": cutoff}})

    # Today's expectancy (Quant agent)
    day0 = now.strftime("%Y-%m-%dT00:00:00")
    today = await db.trades.find(
        {"user_id": uid, "status": "closed", "pnl": {"$ne": None},
         "closed_at": {"$gte": day0}}).to_list(2000)
    wins = [t["pnl"] for t in today if t["pnl"] > 0]
    losses = [t["pnl"] for t in today if t["pnl"] < 0]
    n = len(wins) + len(losses)
    expectancy = None
    if n:
        wr = len(wins) / n
        aw = sum(wins) / len(wins) if wins else 0.0
        al = abs(sum(losses) / len(losses)) if losses else 0.0
        expectancy = {"trades": n, "win_rate_pct": round(100 * wr, 1),
                      "avg_win": round(aw, 2), "avg_loss": round(al, 2),
                      "expectancy_per_trade": round(wr * aw - (1 - wr) * al, 2),
                      "total": round(sum(wins) + sum(losses), 2)}

    return {
        "generated_at": now.isoformat(),
        "symbols": symbols,
        "macro": macro,
        "fed_tone": fed,
        "risk": {
            "active_auto_guards": [
                {"title": (g.get("measure") or {}).get("title"),
                 "type": (g.get("measure") or {}).get("type")} for g in guards],
            "losses_last_30min": recent_losses,
            "loss_cooldown_armed": recent_losses > 0,
        },
        "today_expectancy": expectancy,
        "rl_policy": rl,
    }
