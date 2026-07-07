"""iter-109 · Meta-Learning strategy switcher.

The AI learns WHICH strategy works right now. Each preset (trend_rider,
scalper, mean_reversion, breakout, sniper, balanced) is an arm of a
multi-armed bandit scored on the user's own recent trades:

    score = recency-weighted mean R  +  UCB exploration bonus

Switch triggers (with hysteresis so it doesn't thrash):
  · the active strategy shows ≥3 losses in its last 5 trades AND a
    better-scoring alternative exists, or
  · an alternative beats the active arm by SWITCH_MARGIN with enough data.
Untried strategies keep an exploration bonus so the bandit re-probes them
as the market changes. Opt-in via `meta_strategy_enabled`."""
import logging
import math
from datetime import datetime, timezone

from strategy_presets import PRESETS

logger = logging.getLogger(__name__)

STRATEGIES = ["trend_rider", "scalper", "mean_reversion",
              "breakout", "sniper", "balanced"]
HALF_LIFE_TRADES = 10
UCB_C = 0.8
SWITCH_MARGIN = 0.10
LOSS_STREAK_N = 5
LOSS_STREAK_TRIGGER = 3
LOOKBACK_TRADES = 80
DEFAULT_STRATEGY = "balanced"


def score_strategies(trades: list) -> dict:
    """trades: [{meta_strategy, pnl}] oldest→newest. Returns per-strategy
    {n, mean, ucb, recent_losses} using recency-weighted normalised pnl."""
    pnls = [abs(float(t.get("pnl") or 0)) for t in trades if t.get("pnl")]
    scale = (sum(pnls) / len(pnls)) if pnls else 1.0
    scale = scale or 1.0
    total_n = 0
    stats = {s: {"wsum": 0.0, "w": 0.0, "n": 0, "last": []} for s in STRATEGIES}
    n_all = len(trades)
    for i, t in enumerate(trades):
        s = t.get("meta_strategy")
        if s not in stats or t.get("pnl") is None:
            continue
        age = n_all - 1 - i
        w = 0.5 ** (age / HALF_LIFE_TRADES)
        r = max(-3.0, min(3.0, float(t["pnl"]) / scale))
        stats[s]["wsum"] += w * r
        stats[s]["w"] += w
        stats[s]["n"] += 1
        stats[s]["last"].append(1 if float(t["pnl"]) > 0 else 0)
        total_n += 1
    out = {}
    for s, st in stats.items():
        mean = st["wsum"] / st["w"] if st["w"] > 0 else 0.0
        bonus = UCB_C * math.sqrt(math.log(max(total_n, 2)) / max(st["n"], 1))
        last5 = st["last"][-LOSS_STREAK_N:]
        out[s] = {"n": st["n"], "mean": round(mean, 3),
                  "ucb": round(mean + bonus, 3),
                  "recent_losses": last5.count(0), "recent_n": len(last5)}
    return out


def choose_strategy(scores: dict, current: str | None) -> dict:
    """Bandit selection with hysteresis. Returns {strategy, switched, reason}."""
    if current not in STRATEGIES:
        best = max(scores, key=lambda s: scores[s]["ucb"]) if scores else DEFAULT_STRATEGY
        return {"strategy": best if scores else DEFAULT_STRATEGY,
                "switched": current is not None,
                "reason": "no active strategy — bandit initialised"}
    cur = scores.get(current) or {"ucb": 0.0, "mean": 0.0, "n": 0,
                                  "recent_losses": 0, "recent_n": 0}
    best_alt_key = max((s for s in scores if s != current),
                       key=lambda s: scores[s]["ucb"], default=None)
    best_alt = scores.get(best_alt_key) if best_alt_key else None
    losing = (cur["recent_n"] >= LOSS_STREAK_N
              and cur["recent_losses"] >= LOSS_STREAK_TRIGGER)
    if best_alt and losing and best_alt["ucb"] > cur["ucb"]:
        return {"strategy": best_alt_key, "switched": True,
                "reason": (f"'{current}' lost {cur['recent_losses']} of its last "
                           f"{cur['recent_n']} trades — bandit switches to "
                           f"'{best_alt_key}' (score {best_alt['ucb']} vs "
                           f"{cur['ucb']}).")}
    if (best_alt and cur["n"] >= LOSS_STREAK_N
            and best_alt["ucb"] > cur["ucb"] + SWITCH_MARGIN):
        return {"strategy": best_alt_key, "switched": True,
                "reason": (f"'{best_alt_key}' outperforms '{current}' "
                           f"({best_alt['ucb']} vs {cur['ucb']}, margin "
                           f"{SWITCH_MARGIN}) — market regime shifted.")}
    return {"strategy": current, "switched": False,
            "reason": f"'{current}' holds (score {cur['ucb']})."}


async def _trades_with_strategy(db, user_id: str) -> list:
    from bson import ObjectId
    trades = await db.trades.find(
        {"user_id": user_id, "status": "closed", "pnl": {"$ne": None},
         "origin": "auto"},
        {"pnl": 1, "signal_id": 1, "closed_at": 1},
    ).sort("closed_at", -1).limit(LOOKBACK_TRADES).to_list(LOOKBACK_TRADES)
    trades.reverse()
    sids = []
    for t in trades:
        try:
            sids.append(ObjectId(t["signal_id"]))
        except Exception:
            continue
    strat_by_sig = {}
    if sids:
        async for s in db.signals.find({"_id": {"$in": sids}},
                                       {"meta_strategy": 1}):
            strat_by_sig[str(s["_id"])] = s.get("meta_strategy")
    return [{"pnl": t.get("pnl"),
             "meta_strategy": strat_by_sig.get(str(t.get("signal_id") or ""))}
            for t in trades]


async def apply_meta_strategy(db, user_id: str, cfg: dict) -> tuple[dict, dict | None]:
    if not cfg.get("meta_strategy_enabled"):
        return cfg, None
    state = (cfg.get("meta_strategy_state") or {})
    current = state.get("active")
    trades = await _trades_with_strategy(db, user_id)
    scores = score_strategies(trades)
    pick = choose_strategy(scores, current)
    key = pick["strategy"]
    if pick["switched"] or not state.get("active"):
        now = datetime.now(timezone.utc).isoformat()
        await db.bot_configs.update_one(
            {"_id": cfg["_id"]},
            {"$set": {"meta_strategy_state": {"active": key, "since": now}}})
        if pick["switched"]:
            await db.meta_strategy_events.insert_one(
                {"user_id": user_id, "from": current, "to": key,
                 "reason": pick["reason"], "at": now})
            logger.info("Meta-strategy switch user=%s %s → %s",
                        user_id, current, key)
    preset_cfg = (PRESETS.get(key) or {}).get("config") or {}
    cfg = {**cfg, **preset_cfg, "meta_strategy_active": key,
           "active_preset": f"{key}_meta"}
    return cfg, {"active": key, "switched": pick["switched"],
                 "reason": pick["reason"], "scores": scores}


async def posture_summary(db, user_id: str) -> dict | None:
    cfg = await db.bot_configs.find_one(
        {"user_id": user_id, "active": True, "meta_strategy_enabled": True})
    if not cfg:
        return None
    state = cfg.get("meta_strategy_state") or {}
    trades = await _trades_with_strategy(db, user_id)
    scores = score_strategies(trades)
    events = await db.meta_strategy_events.find(
        {"user_id": user_id}).sort("at", -1).limit(3).to_list(3)
    return {"active": state.get("active") or DEFAULT_STRATEGY,
            "since": state.get("since"),
            "scores": {k: v for k, v in sorted(
                scores.items(), key=lambda kv: -kv[1]["ucb"])},
            "recent_switches": [
                {"from": e.get("from"), "to": e.get("to"), "at": e.get("at"),
                 "reason": e.get("reason")} for e in events]}
