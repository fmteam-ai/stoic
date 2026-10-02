"""iter-141 · Nightly auto-tuning sweep.

Once per 24h per user: run a Bayesian optimization pass for every tunable
(engine, symbol) combo the user's ACTIVE bots actually trade, and when a
proposal passes the out-of-sample selection gate (see `gate_proposal`),
auto-register it as a LOCKED challenger in the Shadow Lab. Shadow models
are then re-evaluated so promotion-ready alerts fire without anyone opening
the UI.

Selection gate (validation.selection_gate) — ALL of:
  · in-sample score gain ≥ MIN_IMPROVEMENT (the optimiser's own claim)
  · OOS net-R improvement vs default > 0 AFTER costs, on bars the GP never saw
  · ≥ TUNER_MIN_OOS_TRADES (default 20) OOS trades
  · Deflated Sharpe ≥ TUNER_MIN_DSR (default 0.95), deflated by the
    CUMULATIVE number of trials logged for that (engine, symbol)
  · PBO ≤ TUNER_MAX_PBO (default 0.2) when ≥ TUNER_PBO_MIN_TRIALS trials
Rejected proposals are kept with status `rejected_by_gate` + the reasons.

Nothing is ever auto-promoted — the P3 gate + human approval still stand.
"""
import logging
from datetime import datetime, timedelta, timezone

from pip_utils import base_symbol
from strategy_engines import PARAM_BOUNDS, resolve_engine
from validation import selection_gate

logger = logging.getLogger("nightly-tuner")

MIN_IMPROVEMENT = 1.0      # score gain (R) vs default required to auto-register
MIN_TRADES = 5             # in-sample trades floor (OOS floor: TUNER_MIN_OOS_TRADES)
MAX_RUNS_PER_USER = 6
PERIOD_HOURS = 24


def combos_from_configs(configs: list) -> list:
    """Pure: active bot configs → sorted unique (engine, symbol) combos
    limited to engines with tunable parameters."""
    out = set()
    for cfg in configs or []:
        engine = resolve_engine(cfg.get("active_preset"))
        if engine not in PARAM_BOUNDS:
            continue
        for s in (cfg.get("symbols") or []):
            out.add((engine, base_symbol(str(s))))
    return sorted(out)


def gate_proposal(prop: dict) -> dict:
    """Pure: decide whether a bayes_opt proposal may be auto-registered.
    In-sample gains alone never pass — the OOS/DSR/PBO checks must too."""
    oos_best = (prop.get("oos") or {}).get("best") or {}
    n_trials = int((prop.get("trial_log") or {}).get("n_trials_total")
                   or prop.get("evaluations") or 0)
    gate = selection_gate(
        oos_improvement=prop.get("oos_improvement"),
        oos_trades=oos_best.get("trades"),
        dsr=(prop.get("dsr") or {}).get("dsr"),
        pbo=(prop.get("pbo") or {}).get("pbo"),
        n_trials=n_trials)
    pre = [
        {"name": f"in_sample_improvement ≥ {MIN_IMPROVEMENT}",
         "value": prop.get("improvement"),
         "passed": (prop.get("improvement") or 0) >= MIN_IMPROVEMENT},
        {"name": f"in_sample_trades ≥ {MIN_TRADES}",
         "value": (prop.get("best") or {}).get("trades"),
         "passed": ((prop.get("best") or {}).get("trades") or 0) >= MIN_TRADES},
    ]
    checks = pre + gate["checks"]
    failed = [c["name"] for c in checks if not c["passed"]]
    return {"accepted": not failed, "checks": checks, "failed": failed,
            "thresholds": gate["thresholds"], "n_trials": n_trials}


async def sweep_user(db, user_id: str) -> dict:
    now = datetime.now(timezone.utc)
    state = await db.quant_tuning_state.find_one({"user_id": user_id})
    if state and state.get("last_run_at"):
        try:
            last = datetime.fromisoformat(state["last_run_at"])
            if now - last < timedelta(hours=PERIOD_HOURS):
                return {"skipped": True, "reason": "ran within 24h"}
        except ValueError:
            pass

    configs = await db.bot_configs.find(
        {"user_id": user_id, "active": True},
        {"active_preset": 1, "symbols": 1}).to_list(200)
    combos = combos_from_configs(configs)[:MAX_RUNS_PER_USER]

    from bayes_opt import run_bayes_optimization
    from model_shadow import evaluate_user_models, register_model

    ran, registered, rejected, errors = 0, [], [], []
    for engine, symbol in combos:
        try:
            prop = await run_bayes_optimization(db, user_id, engine, symbol)
        except ValueError as e:
            errors.append(f"{engine}/{symbol}: {e}")
            continue
        ran += 1
        gate = gate_proposal(prop)
        await db.tuning_proposals.update_one(
            {"user_id": user_id, "version": prop["version"], "symbol": symbol},
            {"$set": {"gate": gate, **({} if gate["accepted"] else
                                       {"status": "rejected_by_gate"})}})
        if not gate["accepted"]:
            rejected.append({"version": prop["version"],
                             "failed": gate["failed"]})
            continue
        model = await register_model(
            db, user_id, engine, symbol, prop["params"],
            source="nightly_bayes",
            note=(f"auto: OOS {prop['oos_improvement']:+}R net of costs "
                  f"vs default, DSR {prop['dsr'].get('dsr')}, "
                  f"PBO {(prop.get('pbo') or {}).get('pbo')}"))
        if not model.get("duplicate"):
            await db.tuning_proposals.update_one(
                {"user_id": user_id, "version": prop["version"],
                 "symbol": symbol},
                {"$set": {"status": "shadow_testing"}})
            registered.append(model["version"])
            logger.info("Nightly tuner: %s auto-registered in Shadow Lab "
                        "(user=%s, OOS %+.2fR)", model["version"], user_id,
                        prop["oos_improvement"])

    # re-evaluate shadow models — fires promotion-ready Telegram alerts
    try:
        await evaluate_user_models(db, user_id)
    except Exception as e:  # noqa: BLE001
        errors.append(f"shadow eval: {e}")

    # canary ladder — advance / hold / auto-rollback running canaries
    try:
        from canary_promotion import evaluate_canaries
        await evaluate_canaries(db, user_id)
    except Exception as e:  # noqa: BLE001
        errors.append(f"canary eval: {e}")

    result = {"ran": ran, "combos": len(combos), "registered": registered,
              "rejected_by_gate": rejected, "errors": errors, "at": now.isoformat()}
    await db.quant_tuning_state.update_one(
        {"user_id": user_id},
        {"$set": {"last_run_at": now.isoformat(), "last_result": result}},
        upsert=True)
    logger.info("Nightly tuner user=%s: %s/%s runs, %s challenger(s) queued",
                user_id, ran, len(combos), len(registered))
    return result


async def sweep_all(db) -> None:
    user_ids = await db.bot_configs.distinct("user_id", {"active": True})
    for uid in user_ids:
        try:
            await sweep_user(db, uid)
        except Exception as e:  # noqa: BLE001
            logger.warning("nightly tuner failed for user %s: %s", uid, e)
