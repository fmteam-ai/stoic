"""Tier 5 — Strategy Marketplace preview.

Each built-in strategy preset becomes an installable card with VERIFIED
performance (from the user's real closed trades attributed by engine scope),
max drawdown, risk class, regime suitability, best-fit broker (from the
broker-intel database) and where it is currently installed.
"""
from datetime import datetime, timedelta, timezone

ENGINE_FOR = {"sniper": "mtf", "balanced": "mtf", "trend_rider": "mtf",
              "scalper": "hf_scalp", "fast_scalp": "hf_scalp_fast",
              "breakout": "breakout_m15", "mean_reversion": "range_fade",
              "adaptive": None}
RISK_CLASS = {"sniper": "CONSERVATIVE", "balanced": "MODERATE",
              "trend_rider": "MODERATE", "scalper": "ACTIVE",
              "fast_scalp": "AGGRESSIVE", "breakout": "ACTIVE",
              "mean_reversion": "MODERATE", "adaptive": "ADAPTIVE"}
REGIME_FIT = {"mtf": "Aligned multi-timeframe trends",
              "hf_scalp": "Active trending sessions",
              "hf_scalp_fast": "High-tempo sessions on tight spreads",
              "breakout_m15": "Volatility expansion / range breaks",
              "range_fade": "Ranging, mean-reverting days"}
STYLE_FOR = {"hf_scalp": "scalping", "hf_scalp_fast": "scalping",
             "breakout_m15": "scalping", "range_fade": "swing",
             "mtf": "swing"}
CAPITAL_FOR = {"CONSERVATIVE": "$5,000+", "MODERATE": "$2,000+",
               "ACTIVE": "$1,000+", "AGGRESSIVE": "$1,000+",
               "ADAPTIVE": "$2,000+"}


def _stars(pf: float | None, n: int) -> int | None:
    if pf is None or n < 3:
        return None
    return 5 if pf >= 2 else 4 if pf >= 1.5 else 3 if pf >= 1.2 \
        else 2 if pf >= 1.0 else 1


async def strategy_cards(db, user_id: str, days: int = 90) -> dict:
    from strategy_presets import list_presets
    from broker_intel import style_suitability

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    perf: dict = {}
    async for t in db.trades.find(
            {"user_id": user_id, "status": "closed", "origin": "auto",
             "closed_at": {"$gte": since}, "pnl": {"$ne": None},
             "scope": {"$ne": None}},
            {"scope": 1, "pnl": 1, "closed_at": 1}).sort("closed_at", 1):
        e = perf.setdefault(t["scope"], {"n": 0, "wins": 0, "pnl": 0.0,
                                         "gross_win": 0.0, "gross_loss": 0.0,
                                         "peak": 0.0, "max_dd": 0.0,
                                         "equity": 0.0})
        pnl = float(t["pnl"])
        e["n"] += 1
        e["pnl"] += pnl
        e["equity"] += pnl
        e["peak"] = max(e["peak"], e["equity"])
        e["max_dd"] = max(e["max_dd"], e["peak"] - e["equity"])
        if pnl > 0:
            e["wins"] += 1
            e["gross_win"] += pnl
        elif pnl < 0:
            e["gross_loss"] += abs(pnl)

    labels = {}
    async for a in db.accounts.find({"user_id": user_id},
                                    {"label": 1, "broker": 1}):
        labels[str(a["_id"])] = a.get("label") or a.get("broker")

    # best broker per style from the newest intel score of each account
    best: dict = {}
    seen_accounts: set = set()
    async for s in db.broker_intel_scores.find({}).sort("at", -1).limit(60):
        acc = str(s.get("account_id"))
        if acc in seen_accounts or acc not in labels:
            continue
        seen_accounts.add(acc)
        comps = {k: (v if isinstance(v, dict) else {"score": v})
                 for k, v in (s.get("components") or {}).items()}
        suit = style_suitability(comps)
        for style, score in suit.items():
            if score is not None and score > best.get(style, (None, -1))[1]:
                best[style] = (labels[acc], score)

    installed: dict = {}
    async for cfg in db.bot_configs.find(
            {"user_id": user_id, "active": True},
            {"active_preset": 1, "account_id": 1}):
        key = cfg.get("active_preset")
        if key:
            installed.setdefault(key, []).append(
                labels.get(str(cfg.get("account_id"))) or "default")

    cards = []
    for p in list_presets():
        key = p["key"]
        engine = ENGINE_FOR.get(key)
        e = perf.get(engine) if engine else None
        performance = None
        if e:
            pf = (round(e["gross_win"] / e["gross_loss"], 2)
                  if e["gross_loss"] else None)
            performance = {"trades": e["n"], "wins": e["wins"],
                           "win_rate": round(e["wins"] / e["n"] * 100, 1),
                           "pnl": round(e["pnl"], 2), "profit_factor": pf,
                           "max_drawdown_usd": round(e["max_dd"], 2)}
        risk_class = RISK_CLASS.get(key, "MODERATE")
        style = STYLE_FOR.get(engine or "", "swing")
        bb = best.get(style)
        cards.append({
            "key": key, "label": p.get("label"),
            "tagline": p.get("tagline"),
            "description": p.get("description"),
            "icon": p.get("icon"), "color": p.get("color"),
            "engine": engine, "creator": "STOIC Core",
            "risk_class": risk_class,
            "regime_fit": REGIME_FIT.get(engine or "",
                                         "Adapts to the current regime"),
            "recommended_capital": CAPITAL_FOR.get(risk_class, "$2,000+"),
            "verified": bool(performance and performance["trades"] >= 3),
            "performance": performance,
            "stars": _stars((performance or {}).get("profit_factor"),
                            (performance or {}).get("trades") or 0),
            "best_broker": ({"label": bb[0], "score": bb[1]} if bb else None),
            "installed_on": installed.get(key, []),
        })
    return {"days": days, "strategies": cards}
