"""Weekly Agent Report Card — per-agent gate activity + estimated P&L impact.

Aggregates the last 7 days of intelligence_counters and grades each agent's
hypothetical impact using the account's realized expected-value per trade:
a gate that blocked N trades "saved" N × |EV| when EV is negative, or
"cost" N × EV of missed profit when EV is positive. It is an estimate, not
a backtest — surfaced so users can make data-driven enforce/advise choices.
"""
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("agent-report-card")

# (display name, slug, hard-block counters, soft/advisory counters)
AGENTS = [
    ("Multi-Timeframe Gate", "mtf", ["mtf_veto"], []),
    ("Master Consensus", "consensus", ["consensus_block"], ["consensus_low"]),
    ("ML Ensemble", "ml_ensemble", ["ml_ensemble_block"], ["ml_ensemble_low_p"]),
    ("Uncertainty Estimator", "uncertainty", ["uncertainty_skip"], ["uncertainty_low_conf"]),
    ("Monte Carlo Simulator", "monte_carlo", ["mc_block"], ["mc_negative_ev"]),
    ("Risk Engine (CVaR)", "risk_engine", ["risk_engine_block"], []),
    ("RL Policy", "rl_policy", ["rl_policy_block"], ["rl_policy_block_advice"]),
    ("Bayes Decision", "bayes", ["bayes_block"], ["bayes_d_quality"]),
    ("Forecast Agent", "forecast", ["forecast_gate_block"], ["forecast_gate_advice"]),
    ("News Gate", "news", ["news_gate_veto"], []),
    ("Calendar Intelligence", "calendar", ["calendar_intel_veto"], []),
    ("Liquidity Map", "liquidity", ["liquidity_gate_veto"], []),
    ("Learned Meta-Labeler", "learned_meta", ["learned_meta_veto"], []),
    ("Structure & Range Gates", "structure", ["structure_gate_veto", "range_gate_veto"], []),
    ("Payoff Guard", "payoff", ["payoff_guard_veto", "final_rr_veto"], ["payoff_guard_tighten"]),
    ("Execution Guards", "execution", ["velocity_veto", "slippage_veto", "spread_block", "rr_veto", "aplus_veto"], []),
    ("Cooldowns & Breakers", "cooldowns",
     ["loss_cooldown_block", "sl_cooldown_block", "eod_quiet_block",
      "auto_guard_block", "auto_tune_block", "fed_tone_veto", "prob_ev_block"],
     ["prob_ev_negative"]),
]

CACHE_TTL_SEC = 3600


async def _baseline_stats(db, user_id: str) -> dict:
    """Realized win-rate / avg win / avg loss / EV from recent closed trades."""
    for days in (7, 30):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = db.trades.find({
            "user_id": user_id, "status": "closed", "origin": "auto",
            "closed_at": {"$gte": cutoff},
            "pnl_estimated": {"$ne": True},
        }, {"pnl": 1})
        pnls = [float(t.get("pnl") or 0) async for t in cur]
        pnls = [p for p in pnls if p != 0]
        if len(pnls) >= 10 or days == 30:
            break
    if not pnls:
        return {"closed_trades": 0, "win_rate": 0, "avg_win": 0,
                "avg_loss": 0, "ev_per_trade": 0, "window_days": days}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(pnls)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    ev = win_rate * avg_win + (1 - win_rate) * avg_loss
    return {
        "closed_trades": len(pnls),
        "win_rate": round(win_rate * 100, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "ev_per_trade": round(ev, 2),
        "window_days": days,
    }


async def _counter_sums(db, user_id: str, days: int = 7) -> dict:
    cutoff_day = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    sums: dict = {}
    async for doc in db.intelligence_counters.find(
            {"user_id": user_id, "day": {"$gte": cutoff_day}}):
        for k, v in doc.items():
            if isinstance(v, (int, float)) and k not in ("_id",):
                sums[k] = sums.get(k, 0) + int(v)
    return sums


async def build_report_card(db, user_id: str) -> dict:
    now = datetime.now(timezone.utc)
    baseline = await _baseline_stats(db, user_id)
    sums = await _counter_sums(db, user_id, days=7)
    ev = float(baseline.get("ev_per_trade") or 0)

    agents = []
    total_blocks = 0
    total_est = 0.0
    for name, slug, hard_keys, soft_keys in AGENTS:
        hard = sum(sums.get(k, 0) for k in hard_keys)
        soft = sum(sums.get(k, 0) for k in soft_keys)
        # A block avoids one expected trade outcome: saved = -EV per block.
        est_impact = round(hard * -ev, 2) if hard else 0.0
        if hard == 0 and soft == 0:
            verdict = "QUIET"
        elif ev < 0:
            verdict = "KEEP ENFORCE"
        elif ev > 0 and hard > 0:
            verdict = "CONSIDER ADVISE"
        else:
            verdict = "MONITORING"
        total_blocks += hard
        total_est += est_impact
        agents.append({
            "name": name, "slug": slug,
            "hard_blocks": hard, "soft_flags": soft,
            "est_pnl_impact": est_impact,
            "verdict": verdict,
        })
    # Busiest agents first, quiet ones last
    agents.sort(key=lambda a: (-(a["hard_blocks"] + a["soft_flags"]), a["name"]))

    return {
        "period": {"from": (now - timedelta(days=7)).isoformat(), "to": now.isoformat()},
        "baseline": baseline,
        "agents": agents,
        "totals": {"hard_blocks": total_blocks, "est_pnl_impact": round(total_est, 2)},
        "built_at": now.isoformat(),
        "method": ("Estimated from realized expected-value per trade "
                   f"(last {baseline.get('window_days')}d, {baseline.get('closed_trades')} closed trades). "
                   "Positive impact = losses likely avoided; negative = winners possibly skipped."),
    }


async def get_report_card(db, user_id: str, force: bool = False) -> dict:
    cached = await db.agent_report_cards.find_one({"user_id": user_id})
    if cached and not force:
        try:
            built = datetime.fromisoformat(str(cached.get("built_at")))
            if (datetime.now(timezone.utc) - built).total_seconds() < CACHE_TTL_SEC:
                cached.pop("_id", None)
                return cached
        except Exception:
            pass
    card = await build_report_card(db, user_id)
    await db.agent_report_cards.update_one(
        {"user_id": user_id}, {"$set": {**card, "user_id": user_id}}, upsert=True)
    return card
