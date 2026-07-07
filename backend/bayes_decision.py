"""iter-65 · Bayesian Decision Model.

Estimates the probability a TRADE SUCCEEDS (not the next candle) with a
Beta-Binomial posterior per market state, plus expected reward/loss in
R-multiples learned from the user's real outcomes:

    BUY · P(success) 72% [CI 61-81%] · E[reward] 2.8R · E[loss] 0.8R
    → EV +1.79R · quality A

States reuse the RL layer's context key (symbol|action|session|regime|
short-alignment) with hierarchical fallback to symbol|action counts.
Prior: Beta(2,2) — weakly informative, shrinks small samples toward 50%."""
from datetime import datetime, timedelta, timezone

from scipy.stats import beta as beta_dist

from pip_utils import base_symbol, price_to_pips
from rl_policy import extract_state

PRIOR_A = 2.0
PRIOR_B = 2.0
MIN_EVIDENCE = 8          # trades needed before enforce may act
LOOKBACK_DAYS = 90
MODEL_TTL_HOURS = 6


def trade_r_multiple(trade: dict, sig: dict) -> float | None:
    """R = pnl / risk$, risk$ estimated from the planned SL distance."""
    pnl = trade.get("pnl")
    entry = trade.get("entry_price")
    sl = (sig or {}).get("stop_loss") or trade.get("stop_loss")
    if pnl is None or not entry or not sl or float(entry) == float(sl):
        return None
    sl_pips = abs(price_to_pips(trade.get("symbol"), float(entry) - float(sl)))
    if sl_pips <= 0:
        return None
    # pnl at SL scales linearly with distance: risk$ = |pnl| * sl_pips / |move_pips|
    exit_p = trade.get("exit_price")
    if not exit_p or float(exit_p) == float(entry):
        return None
    move_pips = abs(price_to_pips(trade.get("symbol"), float(exit_p) - float(entry)))
    if move_pips <= 0:
        return None
    risk_usd = abs(float(pnl)) * sl_pips / move_pips
    if risk_usd <= 0:
        return None
    return float(pnl) / risk_usd


def build_model(trades: list, signals_by_id: dict) -> dict:
    states: dict = {}
    symbols: dict = {}
    for t in trades:
        pnl = t.get("pnl")
        if pnl is None or pnl == 0:
            continue
        sig = signals_by_id.get(str(t.get("signal_id") or "")) or {}
        key = extract_state(t.get("symbol"), t.get("action"), sig)
        skey = f"{base_symbol(t.get('symbol'))}|{t.get('action')}"
        r = trade_r_multiple(t, sig)
        for bucket, k in ((states, key), (symbols, skey)):
            st = bucket.setdefault(k, {"wins": 0, "losses": 0,
                                       "win_r_sum": 0.0, "loss_r_sum": 0.0})
            if pnl > 0:
                st["wins"] += 1
                if r is not None:
                    st["win_r_sum"] += r
            else:
                st["losses"] += 1
                if r is not None:
                    st["loss_r_sum"] += abs(r)
    return {"states": states, "symbols": symbols,
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "trades_used": len(trades),
            "params": {"prior_a": PRIOR_A, "prior_b": PRIOR_B,
                       "min_evidence": MIN_EVIDENCE}}


def _stats(st: dict | None):
    st = st or {"wins": 0, "losses": 0, "win_r_sum": 0.0, "loss_r_sum": 0.0}
    n = st["wins"] + st["losses"]
    a, b = PRIOR_A + st["wins"], PRIOR_B + st["losses"]
    er = st["win_r_sum"] / st["wins"] if st["wins"] else None
    el = st["loss_r_sum"] / st["losses"] if st["losses"] else None
    return n, a, b, er, el


def decision(model: dict, state_key: str, symbol_key: str,
             tp_pips: float | None = None, sl_pips: float | None = None) -> dict:
    n, a, b, er, el = _stats((model.get("states") or {}).get(state_key))
    if n < MIN_EVIDENCE:   # hierarchical fallback to symbol|direction level
        n2, a2, b2, er2, el2 = _stats((model.get("symbols") or {}).get(symbol_key))
        if n2 > n:
            n, a, b = n2, a2, b2
            er, el = er if er is not None else er2, el if el is not None else el2
    p = a / (a + b)
    ci_low, ci_high = (round(float(beta_dist.ppf(q, a, b)), 3) for q in (0.05, 0.95))
    plan_r = (tp_pips / sl_pips) if tp_pips and sl_pips else None
    exp_reward = er if er is not None else (plan_r or 1.0)
    exp_loss = el if el is not None else 1.0
    ev_r = p * exp_reward - (1 - p) * exp_loss
    quality = ("A" if p >= 0.65 and ev_r >= 0.5 else
               "B" if ev_r >= 0.2 else
               "C" if ev_r >= 0 else "D")
    return {"p_success": round(p, 3), "ci90": [ci_low, ci_high], "n": n,
            "expected_reward_r": round(exp_reward, 2),
            "expected_loss_r": round(exp_loss, 2),
            "ev_r": round(ev_r, 2), "quality": quality,
            "state": state_key}


def bayes_decision(model: dict, signal: dict, symbol: str) -> dict:
    action = signal.get("action")
    key = extract_state(symbol, action, signal)
    skey = f"{base_symbol(symbol)}|{action}"
    entry = signal.get("entry_price")
    sl = signal.get("stop_loss")
    tp1 = signal.get("tp1") or signal.get("take_profit")
    tp_pips = sl_pips = None
    if entry and sl and tp1:
        tp_pips = abs(price_to_pips(symbol, float(tp1) - float(entry)))
        sl_pips = abs(price_to_pips(symbol, float(sl) - float(entry)))
    return decision(model, key, skey, tp_pips, sl_pips)


async def get_model(db, user_id: str) -> dict:
    doc = await db.bayes_models.find_one({"user_id": user_id})
    if doc:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(doc["trained_at"])).total_seconds()
            if age < MODEL_TTL_HOURS * 3600:
                return doc
        except (KeyError, ValueError):
            pass
    return await train_model(db, user_id)


async def train_model(db, user_id: str) -> dict:
    from bson import ObjectId
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    trades = await db.trades.find({
        "user_id": user_id, "status": "closed", "pnl": {"$ne": None},
        "origin": "auto", "closed_at": {"$gte": since},
        "pnl_estimated": {"$ne": True}, "pnl_unknown": {"$ne": True},
    }).to_list(5000)
    sids = []
    for t in trades:
        try:
            sids.append(ObjectId(t["signal_id"]))
        except Exception:
            continue
    sigs = {}
    if sids:
        async for s in db.signals.find(
                {"_id": {"$in": sids}},
                {"session": 1, "regime": 1, "mtf_tiers": 1, "stop_loss": 1}):
            sigs[str(s["_id"])] = s
    model = build_model(trades, sigs)
    model["user_id"] = user_id
    await db.bayes_models.update_one(
        {"user_id": user_id}, {"$set": model}, upsert=True)
    return model
