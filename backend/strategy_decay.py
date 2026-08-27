"""Strategy Decay Detector (v59 #4) — every strategy carries a health
state HEALTHY → WATCH → DEGRADED → DECAYING → DISABLED. Decay is NEVER
classified from raw P&L alone: only alpha_clean trades count, and when
recent losses are primarily attributed to execution/broker/infra/news
noise the verdict is softened by one state (that is not alpha decay)."""
import logging
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("strategy.decay")

STATES = ["HEALTHY", "WATCH", "DEGRADED", "DECAYING", "DISABLED"]
MULT = {"HEALTHY": 1.0, "WATCH": 1.0, "DEGRADED": 0.75,
        "DECAYING": 0.5, "DISABLED": 0.0}
NOISE_PRIMARY = {"EXECUTION_ERROR", "BROKER_ERROR",
                 "INFRASTRUCTURE_ERROR", "NEWS_SHOCK"}
MIN_BASE_TRADES = 15
CACHE_TTL_S = 900
RECOVERY_STREAK = 2   # consecutive better evaluations before de-escalating
_cache: dict = {}


def hysteresis_step(prev_state: str | None, raw_state: str,
                    better_streak: int,
                    min_streak: int = RECOVERY_STREAK) -> tuple:
    """Hysteresis on health transitions: degradation applies IMMEDIATELY
    (safety first, jumps allowed); recovery moves at most ONE step per
    evaluation and only after `min_streak` consecutive better readings.
    Returns (effective_state, new_better_streak)."""
    if prev_state not in STATES:
        return raw_state, 0
    pi, ri = STATES.index(prev_state), STATES.index(raw_state)
    if ri >= pi:                       # same or worse → apply raw at once
        return raw_state, 0
    streak = better_streak + 1
    if streak >= min_streak:           # recover one step, reset streak
        return STATES[pi - 1], 0
    return prev_state, streak          # hold the worse state for now


def _state_of(n_flags: int) -> str:
    return STATES[min(int(n_flags), len(STATES) - 1)]


def _pf(rs: list) -> float:
    gw = sum(r for r in rs if r > 0)
    gl = sum(-r for r in rs if r < 0)
    if gl <= 0:
        return 999.0 if gw > 0 else 0.0
    return gw / gl


async def strategy_health(db, user_id: str, scope: str) -> dict:
    from outcome_attribution import result_r
    now = datetime.now(timezone.utc)
    d30 = (now - timedelta(days=30)).isoformat()
    d90 = (now - timedelta(days=90)).isoformat()
    base, recent, recent_loss_primary = [], [], []
    async for t in db.trades.find(
            {"user_id": user_id, "scope": scope, "status": "closed",
             "closed_at": {"$gte": d90}, "alpha_clean": {"$ne": False}},
            {"pnl": 1, "entry_price": 1, "stop_loss": 1, "exit_price": 1,
             "action": 1, "closed_at": 1,
             "attribution_primary": 1}).limit(1500):
        r, _src = result_r(t)
        base.append(r)
        if str(t.get("closed_at") or "") >= d30:
            recent.append(r)
            if r < 0:
                recent_loss_primary.append(
                    str(t.get("attribution_primary") or ""))
    if len(base) < MIN_BASE_TRADES:
        return {"scope": scope, "state": "HEALTHY", "unproven": True,
                "n_base": len(base), "n_recent": len(recent), "flags": [],
                "note": f"unproven — {len(base)}/{MIN_BASE_TRADES} "
                        f"alpha-clean trades in 90d"}
    exp_b = sum(base) / len(base)
    exp_r = sum(recent) / len(recent) if len(recent) >= 5 else exp_b
    pf_b, pf_r = _pf(base), (_pf(recent) if len(recent) >= 5 else _pf(base))
    wr_b = sum(1 for r in base if r > 0) / len(base)
    wr_r = (sum(1 for r in recent if r > 0) / len(recent)
            if len(recent) >= 5 else wr_b)
    wins_r = [r for r in recent if r > 0] or [r for r in base if r > 0]
    losses_r = [-r for r in recent if r < 0] or [-r for r in base if r < 0]
    payoff = ((sum(wins_r) / len(wins_r))
              / max(1e-9, sum(losses_r) / len(losses_r))
              if wins_r and losses_r else None)
    freq_b = len(base) / (90 / 7)
    freq_r = len(recent) / (30 / 7)
    flags = []
    if exp_r < exp_b - 0.15:
        flags.append("expectancy_decline")
    if exp_r < 0 <= exp_b:
        flags.append("negative_expectancy")
    if pf_r < 1.0 <= pf_b:
        flags.append("profit_factor_collapse")
    if wr_r < wr_b - 0.12:
        flags.append("win_rate_drop")
    if len(base) >= 30 and freq_r < 0.4 * freq_b:
        flags.append("signal_frequency_drop")
    noise_frac = (sum(1 for p in recent_loss_primary
                      if p in NOISE_PRIMARY) / len(recent_loss_primary)
                  if recent_loss_primary else 0.0)
    attribution_guard = False
    if noise_frac > 0.5 and flags:
        flags = flags[:-1]   # execution/broker noise, not alpha decay
        attribution_guard = True
    state = _state_of(len(flags))
    raw_state = state
    better_streak = 0
    try:
        prev = await db.strategy_health.find_one(
            {"user_id": user_id, "scope": scope},
            {"state": 1, "better_streak": 1})
        if prev:
            state, better_streak = hysteresis_step(
                prev.get("state"), raw_state,
                int(prev.get("better_streak") or 0))
        await db.strategy_health.update_one(
            {"user_id": user_id, "scope": scope},
            {"$set": {"state": state, "raw_state": raw_state,
                      "better_streak": better_streak,
                      "hysteresis_at": datetime.now(
                          timezone.utc).isoformat()}}, upsert=True)
    except Exception as e:  # noqa: BLE001
        logger.debug("decay hysteresis unavailable: %s", e)
    return {"scope": scope, "state": state, "raw_state": raw_state,
            "better_streak": better_streak, "flags": flags,
            "unproven": False, "attribution_guard": attribution_guard,
            "noise_loss_fraction": round(noise_frac, 2),
            "metrics": {
                "expectancy_recent": round(exp_r, 3),
                "expectancy_base": round(exp_b, 3),
                "profit_factor_recent": round(min(pf_r, 999), 2),
                "profit_factor_base": round(min(pf_b, 999), 2),
                "win_rate_recent": round(wr_r, 3),
                "win_rate_base": round(wr_b, 3),
                "payoff_ratio": round(payoff, 2) if payoff else None,
                "trades_per_week_recent": round(freq_r, 1),
                "trades_per_week_base": round(freq_b, 1)},
            "n_base": len(base), "n_recent": len(recent),
            "at": datetime.now(timezone.utc).isoformat()}


async def health_for(db, user_id: str, scope: str) -> dict:
    """15-min cached lookup for hot decision paths."""
    key = (user_id, scope)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    h = await strategy_health(db, user_id, scope)
    _cache[key] = (time.time(), h)
    return h


async def evaluate_all(db, user_id: str) -> list:
    d90 = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    scopes = await db.trades.distinct(
        "scope", {"user_id": user_id, "status": "closed",
                  "closed_at": {"$gte": d90}})
    out = []
    for scope in scopes:
        if not scope:
            continue
        h = await strategy_health(db, user_id, str(scope))
        out.append(h)
        try:
            await db.strategy_health.update_one(
                {"user_id": user_id, "scope": str(scope)},
                {"$set": {**{k: v for k, v in h.items()
                             if k != "better_streak"}, "user_id": user_id},
                 "$push": {"history": {
                     "$each": [{"state": h["state"],
                                "raw_state": h.get("raw_state"),
                                "at": h.get("at"),
                                "flags": h["flags"]}],
                     "$slice": -50}}},
                upsert=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("strategy health persist failed: %s", e)
    return out
