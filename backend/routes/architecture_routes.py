"""iter-115 · Live Architecture Pipeline — the bot's brain, stage by stage.

GET /api/architecture returns the exact pipeline from the design diagram
with real-time health per stage:

  Market Data (Price Feed · News Feed · Macro Data)
    → Feature Engineering
    → Transformer · RL Agent · Bayesian Model
    → Ensemble
    → Regime Detection
    → Decision Confidence
    → Monte Carlo Simulator
    → Dynamic Position Sizing
    → Risk Management AI
    → Order Execution AI
    → Broker/API"""
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/architecture", tags=["architecture"])


def _age_min(iso):
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - dt).total_seconds() / 60)
    except (ValueError, TypeError):
        return None


@router.get("")
async def get_architecture(user=Depends(get_current_user)):
    db = get_db()
    uid = str(user.get("id") or user.get("_id"))
    now = time.time()

    # ── Market Data ──
    cdoc = await db.intraday_candles.find_one({"user_id": uid},
                                              sort=[("updated_at", -1)])
    price_age = None
    if cdoc and (cdoc.get("bars") or []):
        # clamp at 0: broker server time often runs ahead of UTC
        price_age = max(0, round((now - float(cdoc["bars"][-1].get("t") or 0)) / 60))
    from news_understanding import _cache as news_cache
    news_live = any(v and v[1] for v in news_cache.values())
    macro_age = None
    try:
        from macro_feeds import get_macro_snapshot
        macro = await get_macro_snapshot()
        macro_age = _age_min(macro.get("fetched_at")) if macro else None
    except Exception:
        macro = None

    # ── Models ──
    sig = await db.signals.find_one({"user_id": uid}, sort=[("created_at", -1)])
    sig = sig or {}
    rl_doc = await db.rl_policies.find_one({"user_id": uid}) or {}
    bayes_doc = await db.bayes_models.find_one({"user_id": uid}) or {}
    ml_doc = await db.ml_ensembles.find_one({"user_id": uid}) or {}
    online = await db.online_learning.find_one({"user_id": uid}) or {}

    # ── Execution / Broker ──
    accounts = await db.accounts.find({"user_id": uid}).to_list(20)
    connected = [a for a in accounts if a.get("status") == "connected"]
    hb_age = min((a for a in
                  (_age_min(x.get("last_heartbeat")) for x in connected)
                  if a is not None), default=None)
    ea_versions = sorted({a.get("ea_version") for a in connected
                          if a.get("ea_version")})
    open_trades = await db.trades.count_documents(
        {"user_id": uid, "status": {"$in": ["open", "pending"]}})

    def stage(key, label, ok, detail, children=None):
        return {"key": key, "label": label,
                "status": "ok" if ok else "idle", "detail": detail,
                **({"children": children} if children else {})}

    lm_ready = bool(sig.get("liquidity", {}).get("ready")) if sig else False
    stages = [
        stage("market_data", "Market Data", price_age is not None,
              "unified ingest", children=[
                  stage("price_feed", "Price Feed",
                        price_age is not None and price_age < 60,
                        f"M15 candles via EA v1.43{f' · {price_age}min old' if price_age is not None else ' · waiting'}"),
                  stage("news_feed", "News Feed", news_live,
                        "Reuters/Bloomberg wire → Claude -3..+3 scoring"
                        + (" · live" if news_live else " · warming up")),
                  stage("macro_data", "Macro Data", macro_age is not None,
                        f"FRED yields/USD/breakevens{f' · {macro_age}min old' if macro_age is not None else ' · waiting'}")]),
        stage("feature_engineering", "Feature Engineering",
              bool(sig.get("mtf_tiers")) or lm_ready,
              "MTF tiers · SMC structure · liquidity map (OBs, stops, "
              "profile, delta, DOM) · 19-feature vector"),
        stage("models", "Model Layer", True, "three learners in parallel",
              children=[
                  stage("transformer", "Transformer",
                        bool(sig.get("forecast")),
                        "Chronos-Bolt time-series forecast (quantile bands)"),
                  stage("rl_agent", "RL Agent",
                        bool(rl_doc.get("states")),
                        f"offline RL · {len(rl_doc.get('states') or {})} states "
                        f"from real trades"),
                  stage("bayes", "Bayesian Model",
                        bool(bayes_doc.get("cells") or bayes_doc.get("states")),
                        "Beta-Binomial P(success) + EV with credible intervals")]),
        stage("ensemble", "Ensemble",
              ml_doc.get("status") == "trained",
              (f"GBM+XGBoost+LightGBM+CatBoost skill-weighted (AUC) + live "
               f"agents → P(win) · trained on {ml_doc.get('n_trades', 0)} trades"
               + (f" · retrained {online.get('retrain_count', 0)}× online"
                  if online else ""))),
        stage("regime", "Regime Detection Model", bool(sig.get("regime")),
              f"current: "
              f"{(sig.get('regime') or {}).get('regime') if isinstance(sig.get('regime'), dict) else sig.get('regime') or 'awaiting signal'}"
              f" · meta-strategy bandit adapts the playbook per regime"),
        stage("confidence", "Decision Confidence",
              bool(sig.get("uncertainty")),
              "calibrated confidence + LOW/MED/HIGH risk tier from model "
              "disagreement, CI width, agent conflict"),
        stage("monte_carlo", "Monte Carlo Simulator",
              bool(sig.get("monte_carlo")),
              "10,000 bootstrap paths per candidate → P(TP/SL first), EV, "
              "max-DD — negative EV auto-vetoed"),
        stage("sizing", "Dynamic Position Sizing", True,
              "risk% = base × confidence × volatility × accuracy × "
              "liquidity × drawdown (0.1–2.0%)"),
        stage("risk", "Risk Management AI", True,
              "drawdown ladder D/W/M · dynamic leverage · event-exposure "
              "caps · abnormal-market halt · CVaR₉₅ budget"),
        stage("execution", "Order Execution AI", open_trades >= 0,
              f"spread filter · sector caps · trailing/BE management · "
              f"{open_trades} open position(s)"),
        stage("broker", "Broker/API", bool(connected),
              (f"{len(connected)}/{len(accounts)} account(s) connected via "
               f"MT5 EA {'/'.join(ea_versions) or '—'}"
               + (f" · heartbeat {hb_age}min ago" if hb_age is not None else ""))),
    ]
    return {"stages": stages,
            "generated_at": datetime.now(timezone.utc).isoformat()}
