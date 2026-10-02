"""iter-140 · Shadow model testing (institutional Phase C).

Challenger engine-parameter sets run in continuous shadow against the live
M15 stream with a LOCKED version stamp. Each evaluation replays only the
bars that arrived since the last evaluation (incremental, no lookahead) for
BOTH the challenger and the production baseline captured at registration,
so the A/B window is identical. Nothing executes: pure paper.

Promotion is gated by the PROMOTION_CRITERIA.md P3 shadow gate — a
challenger only becomes live `engine_params` after passing every check AND
explicit human approval via the promote endpoint. Fill reconciliation
measures how honest the replay simulator is against actual broker fills.

Costs: both challenger and baseline replays book NET R — every trade is
charged its round-trip cost in R (validation.resolve_costs: the cost model
locked on the shadow doc at registration, else the symbol default), so the
P2 "profit factor after costs" check really is after costs. Shadow docs
registered before this change carry no `cost_model`; their earlier
increments were frictionless, new increments are charged the default.
"""
import hashlib
import json
import logging
from datetime import datetime, timezone

from ablation import replay_outcome
from bayes_opt import WARMUP, new_replay_state, precompute_features, replay
from strategy_engines import DEFAULT_PARAMS, PARAM_BOUNDS
from validation import resolve_costs

logger = logging.getLogger("model-shadow")

PROMOTE_MIN_DAYS = 14        # P3: ≥ 2 weeks of shadow with the locked version
PROMOTE_MIN_TRADES = 30
PROMOTE_MIN_PF = 1.25        # P2: profit factor after costs
PROMOTE_MAX_DD_R = 20.0      # ≈12% of capital at 0.5–0.6% risk per trade


def params_version(engine: str, params: dict) -> str:
    h = hashlib.sha1(json.dumps({"e": engine, "p": params},
                                sort_keys=True).encode()).hexdigest()[:10]
    return f"{engine}~{h}"


def _clamp_params(engine: str, params: dict) -> dict:
    bounds = PARAM_BOUNDS[engine]
    out = {}
    for k, (lo, hi) in bounds.items():
        v = float(params.get(k, DEFAULT_PARAMS[engine][k]))
        out[k] = round(min(max(v, lo), hi), 4)
    return out


async def register_model(db, user_id: str, engine: str, symbol: str,
                         params: dict, source: str = "manual",
                         note: str = "", costs: dict | None = None) -> dict:
    if engine not in PARAM_BOUNDS:
        raise ValueError(f"engine '{engine}' is not shadow-testable "
                         f"(supported: {sorted(PARAM_BOUNDS)})")
    from pip_utils import base_symbol
    base = base_symbol(symbol)
    clean = _clamp_params(engine, params or {})
    version = params_version(engine, clean)
    # production baseline LOCKED at registration for a fair A/B
    cfg = await db.bot_configs.find_one(
        {"user_id": user_id, "active": True}, {"engine_params": 1}) or {}
    baseline = ((cfg.get("engine_params") or {}).get(engine)
                or dict(DEFAULT_PARAMS[engine]))
    now = datetime.now(timezone.utc)
    doc = {
        "user_id": user_id, "engine": engine, "symbol": base,
        "version": version, "params": clean,
        "baseline_params": baseline,
        "baseline_version": params_version(engine, baseline),
        "source": source, "note": note,
        "cost_model": resolve_costs(base, costs),
        "status": "testing",
        "registered_at": now.isoformat(),
        "evaluated_until_t": int(now.timestamp()),
        "challenger_state": new_replay_state(),
        "baseline_state": new_replay_state(),
        "last_evaluated_at": None,
    }
    existing = await db.shadow_models.find_one(
        {"user_id": user_id, "version": version, "symbol": base,
         "status": "testing"})
    if existing:
        return {**existing, "_id": str(existing["_id"]), "duplicate": True}
    res = await db.shadow_models.insert_one(doc)
    logger.info("Shadow model registered %s on %s (user=%s, source=%s)",
                version, base, user_id, source)
    return {**doc, "_id": str(res.inserted_id)}


async def evaluate_user_models(db, user_id: str) -> list:
    """Incremental replay of every testing model against bars that arrived
    since its last evaluation. Returns updated model list."""
    from pip_utils import base_symbol  # noqa: F401  (symbols stored as base)
    models = await db.shadow_models.find(
        {"user_id": user_id, "status": "testing"}).to_list(100)
    bars_cache: dict = {}
    feats_cache: dict = {}
    out = []
    for m in models:
        sym = m["symbol"]
        if sym not in bars_cache:
            doc = await db.intraday_candles.find_one(
                {"user_id": user_id, "symbol": sym, "timeframe": "M15"},
                {"bars": 1})
            bars_cache[sym] = (doc or {}).get("bars") or []
            feats_cache[sym] = (precompute_features(bars_cache[sym])
                                if len(bars_cache[sym]) > WARMUP else [])
        bars = bars_cache[sym]
        until = int(m.get("evaluated_until_t") or 0)
        start = next((i for i, b in enumerate(bars)
                      if int(b.get("t") or 0) > until), None)
        if bars and start is not None:
            costs = m.get("cost_model") or resolve_costs(sym)
            ch = replay(m["engine"], bars, feats_cache[sym], m["params"],
                        start=start, state=m.get("challenger_state"),
                        costs=costs)
            bl = replay(m["engine"], bars, feats_cache[sym],
                        m["baseline_params"], start=start,
                        state=m.get("baseline_state"), costs=costs)
            m["challenger_state"] = ch
            m["baseline_state"] = bl
            m["evaluated_until_t"] = int(bars[-1]["t"])
            m["last_evaluated_at"] = datetime.now(timezone.utc).isoformat()
            await db.shadow_models.update_one(
                {"_id": m["_id"]},
                {"$set": {k: m[k] for k in (
                    "challenger_state", "baseline_state",
                    "evaluated_until_t", "last_evaluated_at")}})
        m["promotion"] = promotion_status(m)
        if m["promotion"]["ready"] and not m.get("ready_notified"):
            try:
                from notifier import notify_shadow_ready
                await notify_shadow_ready(user_id, m)
            except Exception as e:  # noqa: BLE001
                logger.warning("shadow-ready alert failed: %s", e)
            await db.shadow_models.update_one(
                {"_id": m["_id"]}, {"$set": {"ready_notified": True}})
            m["ready_notified"] = True
        m["_id"] = str(m["_id"])
        out.append(m)
    return out


def promotion_status(model: dict) -> dict:
    """PROMOTION_CRITERIA P3 shadow gate, computed strictly."""
    ch = model.get("challenger_state") or new_replay_state()
    bl = model.get("baseline_state") or new_replay_state()
    try:
        days = (datetime.now(timezone.utc)
                - datetime.fromisoformat(model["registered_at"])).days
    except (KeyError, ValueError):
        days = 0
    # gross_win_r / gross_loss_r are accumulated from NET-of-cost trade R
    # (bayes_opt.replay with costs), so this is profit factor after costs.
    pf = (ch["gross_win_r"] / ch["gross_loss_r"]
          if ch.get("gross_loss_r", 0) > 0 else (999.0 if ch.get("gross_win_r", 0) > 0 else 0.0))
    checks = [
        {"name": f"≥ {PROMOTE_MIN_DAYS} days in shadow", "value": days,
         "passed": days >= PROMOTE_MIN_DAYS},
        {"name": f"≥ {PROMOTE_MIN_TRADES} shadow trades",
         "value": ch["trades"], "passed": ch["trades"] >= PROMOTE_MIN_TRADES},
        {"name": f"profit factor ≥ {PROMOTE_MIN_PF} (after costs)",
         "value": round(pf, 2), "passed": pf >= PROMOTE_MIN_PF},
        {"name": "beats production baseline (net R)",
         "value": f"{ch['total_r']:.1f}R vs {bl['total_r']:.1f}R",
         "passed": ch["total_r"] > bl["total_r"]},
        {"name": f"max drawdown ≤ {PROMOTE_MAX_DD_R}R",
         "value": round(ch.get("max_dd", 0), 1),
         "passed": ch.get("max_dd", 0) <= PROMOTE_MAX_DD_R},
    ]
    return {"checks": checks, "ready": all(c["passed"] for c in checks),
            "days_in_test": days, "profit_factor": round(pf, 2),
            "costs_charged_r": round(float(ch.get("cost_r") or 0.0), 2),
            "cost_model": model.get("cost_model")}


async def promote_model(db, user_id: str, model_id) -> dict:
    from bson import ObjectId
    m = await db.shadow_models.find_one(
        {"_id": ObjectId(str(model_id)), "user_id": user_id})
    if not m:
        raise ValueError("shadow model not found")
    status = promotion_status(m)
    if not status["ready"]:
        failed = [c["name"] for c in status["checks"] if not c["passed"]]
        raise ValueError("promotion gate not passed: " + "; ".join(failed))
    now = datetime.now(timezone.utc).isoformat()
    meta = {"version": m["version"], "promoted_at": now,
            "model_id": str(m["_id"]), "source": m.get("source")}
    res = await db.bot_configs.update_many(
        {"user_id": user_id, "active": True},
        {"$set": {f"engine_params.{m['engine']}": m["params"],
                  f"engine_params_meta.{m['engine']}": meta}})
    await db.shadow_models.update_one(
        {"_id": m["_id"]}, {"$set": {"status": "promoted", "promoted_at": now}})
    await db.shadow_models.update_many(
        {"user_id": user_id, "engine": m["engine"],
         "status": "promoted", "_id": {"$ne": m["_id"]}},
        {"$set": {"status": "retired", "retired_reason": "superseded"}})
    logger.info("Shadow model PROMOTED %s (user=%s, %s configs updated)",
                m["version"], user_id, res.modified_count)
    return {"version": m["version"], "engine": m["engine"],
            "params": m["params"], "configs_updated": res.modified_count}


async def retire_model(db, user_id: str, model_id, reason: str = "manual") -> dict:
    from bson import ObjectId
    m = await db.shadow_models.find_one(
        {"_id": ObjectId(str(model_id)), "user_id": user_id})
    if not m:
        raise ValueError("shadow model not found")
    await db.shadow_models.update_one(
        {"_id": m["_id"]},
        {"$set": {"status": "retired", "retired_reason": reason}})
    if m.get("status") == "promoted":
        # rolling back a promoted model restores engine defaults
        await db.bot_configs.update_many(
            {"user_id": user_id, "active": True},
            {"$unset": {f"engine_params.{m['engine']}": "",
                        f"engine_params_meta.{m['engine']}": ""}})
    return {"version": m["version"], "status": "retired"}


async def reconcile_fills(db, user_id: str, days: int = 14) -> dict:
    """Simulator honesty check: replay each closed auto trade's entry/SL/TP
    against the M15 bars that actually followed and compare the simulated
    outcome with the broker's realized fill."""
    from datetime import timedelta
    from pip_utils import base_symbol
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    trades = await db.trades.find({
        "user_id": user_id, "status": "closed", "origin": "auto",
        "pnl": {"$ne": None}, "closed_at": {"$gte": since},
        "entry_price": {"$ne": None}, "stop_loss": {"$ne": None},
    }).to_list(1000)

    bars_cache: dict = {}
    matched = mismatched = skipped = 0
    r_gaps: list = []
    rows = []
    for t in trades:
        base = base_symbol(t.get("symbol") or "")
        if base not in bars_cache:
            doc = await db.intraday_candles.find_one(
                {"user_id": user_id, "symbol": base, "timeframe": "M15"},
                {"bars": 1})
            bars_cache[base] = (doc or {}).get("bars") or []
        opened = t.get("opened_at") or t.get("created_at")
        try:
            epoch = datetime.fromisoformat(
                str(opened).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            skipped += 1
            continue
        after = [b for b in bars_cache[base] if int(b.get("t") or 0) > epoch]
        sim = replay_outcome(str(t.get("action") or "").upper(),
                             float(t["entry_price"]), float(t["stop_loss"]),
                             float(t.get("take_profit") or 0), after)
        if sim is None or sim["outcome"] == "timeout":
            skipped += 1
            continue
        actual_win = float(t.get("pnl") or 0) > 0
        sim_win = sim["outcome"] == "tp_first"
        agree = actual_win == sim_win
        matched += agree
        mismatched += not agree
        risk = float(t.get("risk_amount") or 0)
        actual_r = round(float(t["pnl"]) / risk, 2) if risk > 0 else None
        if actual_r is not None:
            r_gaps.append(abs(actual_r - sim["r"]))
        if not agree:
            rows.append({"symbol": t.get("symbol"),
                         "action": t.get("action"),
                         "closed_at": t.get("closed_at"),
                         "actual_pnl": t.get("pnl"), "actual_r": actual_r,
                         "sim_outcome": sim["outcome"], "sim_r": sim["r"]})
    resolved = matched + mismatched
    return {
        "days": days, "trades_considered": len(trades),
        "resolved": resolved, "skipped": skipped,
        "match_rate_pct": round(matched / resolved * 100, 1) if resolved else None,
        "avg_abs_r_gap": round(sum(r_gaps) / len(r_gaps), 2) if r_gaps else None,
        "mismatches": rows[:20],
        "note": ("Each broker fill replayed through the shadow simulator on "
                 "the same bars. High match rate = shadow results are "
                 "trustworthy; a big gap means slippage/fill drift the "
                 "simulator can't see."),
    }
