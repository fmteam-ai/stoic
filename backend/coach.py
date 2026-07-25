"""Tier 11 — AI Coach: proactive explanations that teach the user.

Deterministic cards composed from live system state: why the bot is waiting,
why setups were rejected, why risk changed, what the last lesson was, and how
trustworthy the stated confidence is. No LLM cost — every claim is backed by
recorded evidence.
"""
from datetime import datetime, timedelta, timezone

STAGE_EXPLAIN = {
    "anti_pyramid": "a position was already open on this symbol — the bot never stacks new risk on top of an open trade",
    "anti_tilt": "recent losses on the account triggered the anti-tilt pause that breaks losing spirals",
    "cooldown": "a recent loss put this symbol+direction on cooldown so it can't be re-entered impulsively",
    "monte_carlo_gate": "10,000 simulated price paths priced the setup at negative expected value after costs",
    "news_gate": "the AI news layer read the live narrative as strongly against this direction",
    "narrative_risk": "a live macro narrative made this direction unusually risky, so size or entry was cut",
    "risk_engine": "the risk engine's exposure / drawdown / CVaR budget would have been exceeded",
    "spread_guard": "the spread was too wide — entering would hand the edge to the broker",
    "slippage_guard": "recent fills slipped too far from requested prices on this account",
    "session_trend_gate": "the trade fought the session's established intraday trend",
    "exhaustion_chase_gate": "price was already stretched at the day's extreme — chasing there is poor odds",
    "structure_gate": "market structure (fresh break of structure / supply-demand zone) opposed the entry",
    "correlation_guard": "a correlated account had just taken the same trade — mirroring required proof of profit first",
    "calendar_guard": "a high-impact economic event was too close — first spike is usually a trap",
    "friday_flat": "Friday close window — positions are not opened into the weekend gap",
    "eod_quiet": "end-of-day quiet window — spreads widen drastically before the daily close",
    "drawdown_guard": "the account hit its daily/weekly/monthly drawdown limit and trading paused for the window",
    "daily_loss_guard": "today's realized loss reached the safety cap for the account",
    "exposure_cap": "total open exposure would have exceeded the account's event/exposure budget",
    "payoff_guard": "the final stop/target geometry no longer paid enough for the risk taken",
    "rr_guard": "reward-to-risk fell below the floor after all overlays — not worth taking",
    "knife_filter": "price had just broken down sharply — catching falling knives is disabled",
    "kill_switch": "the platform kill-switch was engaged",
    "safety_guardian": "the safety guardian judged aggregate account risk too high",
    "gate": "a protective gate vetoed the setup",
}


def _explain(stage: str) -> str:
    return STAGE_EXPLAIN.get(stage, STAGE_EXPLAIN["gate"])


async def coach_cards(db, user_id: str) -> dict:
    now = datetime.now(timezone.utc)
    since24 = (now - timedelta(hours=24)).isoformat()

    labels = {}
    async for a in db.accounts.find({"user_id": user_id},
                                    {"label": 1, "broker": 1}):
        labels[str(a["_id"])] = a.get("label") or a.get("broker")

    # 1 · why the bot is waiting
    waiting = []
    async for cfg in db.bot_configs.find(
            {"user_id": user_id, "active": True},
            {"account_id": 1, "_last_notable_pulse": 1, "_last_pulse": 1}):
        p = cfg.get("_last_notable_pulse") or cfg.get("_last_pulse")
        if p and p.get("reason"):
            waiting.append({
                "account": labels.get(str(cfg.get("account_id"))) or "default",
                "symbol": p.get("symbol"),
                "reason": str(p["reason"])[:200], "ts": p.get("ts")})

    # 2 · why setups were rejected (24h, grouped by gate)
    agg = db.trade_decisions.aggregate([
        {"$match": {"user_id": user_id, "status": "rejected",
                    "ts": {"$gte": since24}}},
        {"$group": {"_id": "$stage", "count": {"$sum": 1},
                    "example": {"$last": "$reason"},
                    "symbol": {"$last": "$symbol"}}},
        {"$sort": {"count": -1}}, {"$limit": 8}])
    rejected = []
    async for g in agg:
        rejected.append({"stage": g["_id"], "count": g["count"],
                         "symbol": g.get("symbol"),
                         "coach": _explain(g["_id"]),
                         "example": str(g.get("example") or "")[:180]})

    # 3 · why risk changed (latest adaptive sizing verdict)
    risk = None
    sig = await db.signals.find_one(
        {"user_id": user_id, "adaptive_sizing": {"$ne": None}},
        sort=[("_id", -1)])
    if sig:
        ad = sig["adaptive_sizing"]
        comps = ad.get("components") or {}
        drivers = []
        for name, mult in comps.items():
            try:
                m = float(mult)
            except (TypeError, ValueError):
                continue
            if m < 0.95:
                drivers.append(f"{name} cut size ×{m:.2f}")
            elif m > 1.05:
                drivers.append(f"{name} raised size ×{m:.2f}")
        risk = {"symbol": sig.get("symbol"),
                "multiplier": ad.get("multiplier"),
                "risk_pct": ad.get("risk_pct"),
                "drawdown_frac": ad.get("drawdown_frac"),
                "recent_win_rate": ad.get("recent_win_rate"),
                "drivers": drivers[:4], "ts": sig.get("created_at"),
                "coach": ("Risk per trade is scaled continuously: calibrated "
                          "confidence, volatility, recent accuracy, liquidity "
                          "and drawdown each move the dial.")}

    # 4 · latest lesson from self-evaluation
    lesson = None
    ev = await db.trade_evaluations.find_one(
        {"user_id": user_id, "lesson": {"$nin": [None, ""]}},
        sort=[("_id", -1)])
    if ev:
        mistakes: dict = {}
        async for e in db.trade_evaluations.find(
                {"user_id": user_id}, {"mistakes": 1}).sort(
                "_id", -1).limit(20):
            for m in (e.get("mistakes") or []):
                mistakes[m] = mistakes.get(m, 0) + 1
        top = sorted(mistakes.items(), key=lambda x: -x[1])[:3]
        lesson = {"symbol": ev.get("symbol"), "outcome": ev.get("outcome"),
                  "lesson": ev.get("lesson"),
                  "top_mistakes": [{"mistake": m, "count": c}
                                   for m, c in top]}

    # 5 · how honest is the stated confidence
    confidence = None
    try:
        from calibration import _table_for
        table = await _table_for(db, user_id) or {}
        tot = stated = realized = 0
        for ent in table.values():
            for b in ent["buckets"]:
                tot += b["n"]
                stated += b["stated"] * b["n"]
                realized += b["realized"] * b["n"]
        if tot:
            confidence = {
                "predicted": round(stated / tot, 1),
                "actual": round(realized / tot, 1), "n": tot,
                "coach": ("When the bot says X% it should win X% of the "
                          "time — this compares its claims to reality.")}
    except Exception:  # noqa: BLE001
        confidence = None

    return {"generated_at": now.isoformat(),
            "waiting": waiting[:6], "rejected": rejected,
            "risk": risk, "lesson": lesson, "confidence": confidence}
