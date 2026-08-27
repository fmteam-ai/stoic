"""Meta-Decision Engine — the brain ABOVE individual strategies. It does
not produce BUY/SELL; it decides whether another model should be trusted
RIGHT NOW, producing a 0-100 scorecard, an uncertainty estimate and a
TRADE / REDUCE / SKIP decision with a downscale-only risk multiplier.
Sits beneath the hard deterministic risk envelope — it can only reduce."""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("meta.decision")

W = {"opportunity_quality": 0.25, "strategy_reliability": 0.20,
     "regime_compatibility": 0.20, "execution_quality": 0.15,
     "risk_environment": 0.20}
SKIP_BELOW = 40
REDUCE_BELOW = 70


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp100(v):
    return round(max(0.0, min(100.0, v)), 1)


async def _opportunity(signal: dict) -> float:
    conf = float(signal.get("confidence") or 50)
    cal = signal.get("calibrated_p_win") or {}
    ev = cal.get("ev_r")
    score = conf
    if ev is not None:
        score = 0.6 * conf + 0.4 * _clamp100(50 + float(ev) * 100)
    return _clamp100(score)


async def _reliability(db, user_id: str, scope: str) -> float:
    """Recent-vs-baseline expectancy for this strategy (decay proxy) plus
    calibration sample depth."""
    from outcome_attribution import result_r
    now = datetime.now(timezone.utc)
    d30 = (now - timedelta(days=30)).isoformat()
    d90 = (now - timedelta(days=90)).isoformat()
    recent, base = [], []
    async for t in db.trades.find(
            {"user_id": user_id, "scope": scope, "status": "closed",
             "closed_at": {"$gte": d90}, "alpha_clean": {"$ne": False}},
            {"pnl": 1, "entry_price": 1, "stop_loss": 1, "exit_price": 1,
             "action": 1, "closed_at": 1}).limit(1000):
        r, _s = result_r(t)
        base.append(r)
        if str(t.get("closed_at") or "") >= d30:
            recent.append(r)
    if len(base) < 10:
        return 60.0   # unproven, neutral-slightly-cautious
    base_e = sum(base) / len(base)
    rec_e = sum(recent) / len(recent) if len(recent) >= 5 else base_e
    drift = rec_e - base_e   # negative = degradation
    score = 70 + base_e * 40 + min(0.0, drift) * 60
    return _clamp100(score)


async def _execution_quality(db, user_id: str, symbol: str) -> float:
    """Recent slippage + latency evidence; neutral 75 without data."""
    d14 = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    slips, totals = [], []
    async for t in db.trades.find(
            {"user_id": user_id, "symbol": symbol, "status": "closed",
             "closed_at": {"$gte": d14}},
            {"slippage_pips": 1, "latency_trace": 1}).limit(200):
        if t.get("slippage_pips") is not None:
            slips.append(float(t["slippage_pips"]))
        lt = t.get("latency_trace") or {}
        if lt.get("t7_ms") and lt.get("t9_ms"):
            totals.append(int(lt["t9_ms"]) - int(lt["t7_ms"]))
    score = 75.0
    if slips:
        avg = sum(slips) / len(slips)
        score -= min(35.0, avg * 4)         # ~9 pips avg slippage → floor
    if totals:
        p50 = sorted(totals)[len(totals) // 2]
        score -= min(20.0, max(0.0, (p50 - 500) / 100))
    return _clamp100(score)


async def _risk_environment(db, account: dict | None) -> float:
    try:
        from trading_authority import enforce_new_trade
        gate = await enforce_new_trade(db, account=account)
    except Exception:
        return 70.0
    if not gate.get("ok"):
        return 15.0
    level = str(gate.get("level") or "FULL")
    return {"FULL": 92.0, "REDUCED": 55.0}.get(level, 35.0)


async def meta_decide(db, user_id: str, signal: dict,
                      account: dict | None = None,
                      account_id: str | None = None) -> dict:
    scope = str(signal.get("scope") or signal.get("origin") or "ai")
    symbol = str(signal.get("symbol") or "")
    from regime_intelligence import market_state
    from strategy_router import _family_of, route
    from uncertainty_engine import assess
    state = await market_state(db, user_id, symbol, account_id=account_id)
    fp_key = state.get("fingerprint_key") or "UNKNOWN"
    routing = await route(db, user_id, fp_key)
    fam = _family_of(scope)
    w = routing["weights"].get(fam, 1.0 / 3)
    regime_comp = _clamp100(w / max(routing["weights"].values()) * 100)
    unc_cost = None
    try:
        from transaction_costs import expected_cost_r
        unc_cost = await expected_cost_r(db, user_id, symbol,
                                         signal=signal, scope=scope)
        unc = await assess(db, user_id, signal,
                           cost_r=float(unc_cost["required_edge_r"]))
    except Exception:  # noqa: BLE001
        unc = await assess(db, user_id, signal)
    dims = {
        "opportunity_quality": await _opportunity(signal),
        "strategy_reliability": await _reliability(db, user_id, scope),
        "regime_compatibility": regime_comp,
        "execution_quality": await _execution_quality(db, user_id, symbol),
        "risk_environment": await _risk_environment(db, account),
    }
    uncertainty = float(unc.get("uncertainty") or 0.5)
    composite = sum(dims[k] * W[k] for k in W) * (1 - 0.35 * uncertainty)
    composite = _clamp100(composite)
    if unc.get("hard_gate") or composite < SKIP_BELOW:
        decision, mult = "SKIP", 0.0
    elif composite < REDUCE_BELOW:
        decision = "REDUCE"
        mult = round(max(0.25, min(0.9, composite / 100)), 2)
    else:
        decision = "TRADE"
        mult = round(min(1.0, 0.5 + composite / 200), 2)
    # Strategy Decay Detector — health can only reduce or disable
    health = None
    try:
        from strategy_decay import MULT as _HMULT, health_for
        health = await health_for(db, user_id, scope)
        if health and not health.get("unproven"):
            hm = float(_HMULT.get(health["state"], 1.0))
            if decision != "SKIP":
                if hm <= 0:
                    decision, mult = "SKIP", 0.0
                elif hm < 1.0:
                    mult = round(mult * hm, 2)
                    if decision == "TRADE":
                        decision = "REDUCE"
    except Exception as e:  # noqa: BLE001
        logger.debug("strategy health unavailable: %s", e)
    out = {"decision": decision, "risk_multiplier": mult,
           "composite": composite, "scorecard": dims,
           "uncertainty": uncertainty, "uncertainty_detail": unc,
           "market_state": {"fingerprint_key": fp_key,
                            "labels": state.get("labels"),
                            "vector": state.get("vector"),
                            "session": state.get("session")},
           "router": {"weights": routing["weights"], "family": fam},
           "strategy_health": ({"state": health.get("state"),
                                "flags": health.get("flags")}
                               if health else None),
           "transaction_cost": ({"cost_r": unc_cost.get("cost_r"),
                                 "required_edge_r":
                                 unc_cost.get("required_edge_r")}
                                if unc_cost else None),
           "decision_id": signal.get("decision_id"),
           "engine_version": 2, "at": _now()}
    try:
        await db.meta_decisions.insert_one(
            {**out, "user_id": user_id, "symbol": symbol, "scope": scope,
             "signal_confidence": signal.get("confidence")})
        out.pop("_id", None)
    except Exception as e:
        logger.warning("meta decision persist failed: %s", e)
    return out
