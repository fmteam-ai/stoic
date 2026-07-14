"""iter-141 · Nightly auto-tuning sweep.

Once per 24h per user: run a Bayesian optimization pass for every tunable
(engine, symbol) combo the user's ACTIVE bots actually trade, and when a
proposal beats the default parameters by a meaningful margin, auto-register
it as a LOCKED challenger in the Shadow Lab. Shadow models are then
re-evaluated so promotion-ready alerts fire without anyone opening the UI.

Nothing is ever auto-promoted — the P3 gate + human approval still stand.
"""
import logging
from datetime import datetime, timedelta, timezone

from pip_utils import base_symbol
from strategy_engines import PARAM_BOUNDS, resolve_engine

logger = logging.getLogger("nightly-tuner")

MIN_IMPROVEMENT = 1.0      # score gain (R) vs default required to auto-register
MIN_TRADES = 5             # proposal must have a real sample in the replay
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

    ran, registered, errors = 0, [], []
    for engine, symbol in combos:
        try:
            prop = await run_bayes_optimization(db, user_id, engine, symbol)
        except ValueError as e:
            errors.append(f"{engine}/{symbol}: {e}")
            continue
        ran += 1
        if (prop["improvement"] >= MIN_IMPROVEMENT
                and prop["best"]["trades"] >= MIN_TRADES):
            model = await register_model(
                db, user_id, engine, symbol, prop["params"],
                source="nightly_bayes",
                note=f"auto: +{prop['improvement']}R score vs default")
            if not model.get("duplicate"):
                await db.tuning_proposals.update_one(
                    {"user_id": user_id, "version": prop["version"],
                     "symbol": symbol},
                    {"$set": {"status": "shadow_testing"}})
                registered.append(model["version"])
                logger.info("Nightly tuner: %s auto-registered in Shadow Lab "
                            "(user=%s, +%.2fR)", model["version"], user_id,
                            prop["improvement"])

    # re-evaluate shadow models — fires promotion-ready Telegram alerts
    try:
        await evaluate_user_models(db, user_id)
    except Exception as e:  # noqa: BLE001
        errors.append(f"shadow eval: {e}")

    result = {"ran": ran, "combos": len(combos), "registered": registered,
              "errors": errors, "at": now.isoformat()}
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
