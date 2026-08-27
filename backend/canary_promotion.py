"""Canary promotion ladder — a shadow-qualified challenger NEVER jumps to
100% production, even after human approval:

  Shadow qualified → human approval → 5% → 10% → 25% → 50% → 100%

The allocation is a per-signal probability draw of the challenger params.
Each stage must hold MIN_STAGE_HOURS and MIN_STAGE_TRADES; automatic
rollback fires whenever the challenger's realized distribution deviates
materially from the expectation captured during shadow."""
import logging
from datetime import datetime, timezone

logger = logging.getLogger("canary.promotion")

LADDER = [5, 10, 25, 50, 100]
MIN_STAGE_TRADES = 10
MIN_STAGE_HOURS = 24
WIN_RATE_TOLERANCE = 0.15   # realized win-rate may lag expectation by ≤15pp
AVG_R_TOLERANCE = 0.5       # realized avg R may lag expectation by ≤0.5R


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expected(state: dict) -> dict:
    trades = int(state.get("trades") or 0)
    wins = int(state.get("wins") or 0)
    total_r = float(state.get("total_r") or 0)
    return {"trades": trades,
            "win_rate": round(wins / trades, 3) if trades else 0.0,
            "avg_r": round(total_r / trades, 3) if trades else 0.0}


async def _apply_allocation(db, user_id: str, m: dict, pct: int) -> int:
    res = await db.bot_configs.update_many(
        {"user_id": user_id, "active": True},
        {"$set": {f"engine_params_canary.{m['engine']}": {
            "params": m["params"], "allocation_pct": int(pct),
            "model_id": str(m["_id"]), "version": m["version"]}}})
    return int(res.modified_count or res.matched_count or 0)


async def _clear_allocation(db, user_id: str, engine: str) -> None:
    await db.bot_configs.update_many(
        {"user_id": user_id, "active": True},
        {"$unset": {f"engine_params_canary.{engine}": ""}})


async def start_canary(db, user_id: str, model_id) -> dict:
    """Human approval starts the ladder at 5% — never 100%."""
    from bson import ObjectId
    from model_shadow import promotion_status
    m = await db.shadow_models.find_one(
        {"_id": ObjectId(str(model_id)), "user_id": user_id})
    if not m:
        raise ValueError("shadow model not found")
    if m.get("status") == "canary":
        raise ValueError("canary already running for this model")
    status = promotion_status(m)
    if not status["ready"]:
        failed = [c["name"] for c in status["checks"] if not c["passed"]]
        raise ValueError("promotion gate not passed: " + "; ".join(failed))
    # Champion/Challenger 2.0 — institutional qualification scorecard
    # (purged WF, CPCV, Monte Carlo). qualified=None (insufficient replay
    # data) is advisory; an explicit False BLOCKS the ladder.
    try:
        from champion_challenger2 import qualify
        q = await qualify(db, user_id, str(m["_id"]))
    except Exception as e:  # noqa: BLE001
        logger.warning("qualification 2.0 unavailable (%s) — advisory", e)
        q = None
    if q and q.get("qualified") is False:
        failed = [c["name"] for c in q.get("checks", [])
                  if not c["passed"]]
        raise ValueError("qualification 2.0 not passed: "
                         + "; ".join(failed))
    active_cfgs = await db.bot_configs.count_documents(
        {"user_id": user_id, "active": True})
    if not active_cfgs:
        raise ValueError("no active bot config — the canary allocation "
                         "would influence nothing; activate a bot first")
    canary = {"stage_idx": 0, "allocation_pct": LADDER[0],
              "started_at": _now(), "stage_started_at": _now(),
              "expected": _expected(m.get("challenger_state") or {}),
              "history": []}
    await db.shadow_models.update_one(
        {"_id": m["_id"]}, {"$set": {"status": "canary", "canary": canary}})
    configs = await _apply_allocation(db, user_id, m, LADDER[0])
    logger.info("canary STARTED %s at %d%% (user=%s)", m["version"],
                LADDER[0], user_id)
    return {"version": m["version"], "engine": m["engine"],
            "allocation_pct": LADDER[0], "ladder": LADDER,
            "configs_updated": configs,
            "expected": canary["expected"],
            "note": "shadow-qualified challenger starts at 5% risk "
                    "allocation — advances only with stable evidence"}


async def realized_stats(db, model_id: str, since_iso: str) -> dict:
    sig_ids = [str(s["_id"]) async for s in db.signals.find(
        {"canary.model_id": str(model_id),
         "created_at": {"$gte": since_iso}}, {"_id": 1}).limit(2000)]
    trades = wins = 0
    total_r = pnl_sum = 0.0
    if sig_ids:
        async for t in db.trades.find(
                {"signal_id": {"$in": sig_ids}, "status": "closed"},
                {"pnl": 1, "entry_price": 1, "stop_loss": 1,
                 "exit_price": 1, "action": 1}):
            trades += 1
            p = float(t.get("pnl") or 0)
            pnl_sum += p
            if p > 0:
                wins += 1
            from outcome_attribution import result_r
            r, _src = result_r(t)
            total_r += r
    return {"trades": trades, "signals": len(sig_ids),
            "win_rate": round(wins / trades, 3) if trades else None,
            "avg_r": round(total_r / trades, 3) if trades else None,
            "pnl": round(pnl_sum, 2)}


def deviation_verdict(expected: dict, realized: dict) -> dict:
    """Material-deviation check — only judged with enough evidence."""
    if (realized.get("trades") or 0) < MIN_STAGE_TRADES:
        return {"deviated": False, "judged": False,
                "reason": f"insufficient evidence "
                          f"({realized.get('trades', 0)}/"
                          f"{MIN_STAGE_TRADES} trades)"}
    reasons = []
    if (realized["win_rate"] is not None
            and realized["win_rate"]
            < expected["win_rate"] - WIN_RATE_TOLERANCE):
        reasons.append(f"win rate {realized['win_rate']} vs expected "
                       f"{expected['win_rate']} (tolerance "
                       f"{WIN_RATE_TOLERANCE})")
    if (realized["avg_r"] is not None
            and realized["avg_r"] < expected["avg_r"] - AVG_R_TOLERANCE):
        reasons.append(f"avg R {realized['avg_r']} vs expected "
                       f"{expected['avg_r']} (tolerance {AVG_R_TOLERANCE})")
    return {"deviated": bool(reasons), "judged": True,
            "reason": "; ".join(reasons) or "within expected distribution"}


async def rollback_canary(db, user_id: str, m: dict, reason: str,
                          automatic: bool = False) -> dict:
    canary = m.get("canary") or {}
    await db.shadow_models.update_one(
        {"_id": m["_id"]},
        {"$set": {"status": "rolled_back", "rolled_back_at": _now(),
                  "rollback_reason": reason,
                  "rollback_automatic": automatic,
                  "canary.ended_at": _now()}})
    await _clear_allocation(db, user_id, m["engine"])
    logger.warning("canary ROLLED BACK %s at %s%% (%s): %s", m["version"],
                   canary.get("allocation_pct"),
                   "automatic" if automatic else "manual", reason)
    if automatic:
        try:
            from alerting import raise_alert
            await raise_alert(
                db, "canary_rollback", "critical",
                f"Canary {m['version']} auto-rolled back at "
                f"{canary.get('allocation_pct')}% allocation: {reason}",
                dedup_key=f"canary_rollback_{m['_id']}")
        except Exception as e:  # noqa: BLE001
            logger.warning("canary rollback alert failed: %s", e)
    return {"version": m["version"], "status": "rolled_back",
            "reason": reason, "automatic": automatic}


async def _advance(db, user_id: str, m: dict, realized: dict) -> dict:
    canary = m["canary"]
    idx = int(canary.get("stage_idx") or 0)
    entry = {"allocation_pct": canary["allocation_pct"],
             "realized": realized,
             "stage_started_at": canary.get("stage_started_at"),
             "advanced_at": _now()}
    if idx + 1 >= len(LADDER):   # 100% reached and held → full promotion
        now = _now()
        meta = {"version": m["version"], "promoted_at": now,
                "model_id": str(m["_id"]), "source": m.get("source"),
                "via": "canary_ladder"}
        await db.bot_configs.update_many(
            {"user_id": user_id, "active": True},
            {"$set": {f"engine_params.{m['engine']}": m["params"],
                      f"engine_params_meta.{m['engine']}": meta}})
        await _clear_allocation(db, user_id, m["engine"])
        await db.shadow_models.update_one(
            {"_id": m["_id"]},
            {"$set": {"status": "promoted", "promoted_at": now,
                      "canary.ended_at": now},
             "$push": {"canary.history": entry}})
        await db.shadow_models.update_many(
            {"user_id": user_id, "engine": m["engine"],
             "status": "promoted", "_id": {"$ne": m["_id"]}},
            {"$set": {"status": "retired",
                      "retired_reason": "superseded"}})
        logger.info("canary COMPLETED — %s promoted to champion (user=%s)",
                    m["version"], user_id)
        return {"version": m["version"], "status": "promoted",
                "allocation_pct": 100}
    new_pct = LADDER[idx + 1]
    await db.shadow_models.update_one(
        {"_id": m["_id"]},
        {"$set": {"canary.stage_idx": idx + 1,
                  "canary.allocation_pct": new_pct,
                  "canary.stage_started_at": _now()},
         "$push": {"canary.history": entry}})
    await _apply_allocation(db, user_id, m, new_pct)
    logger.info("canary ADVANCED %s → %d%% (user=%s)", m["version"],
                new_pct, user_id)
    return {"version": m["version"], "status": "canary",
            "allocation_pct": new_pct}


async def evaluate_canaries(db, user_id: str) -> list:
    """Advance, hold or auto-rollback every running canary."""
    out = []
    async for m in db.shadow_models.find(
            {"user_id": user_id, "status": "canary"}):
        canary = m.get("canary") or {}
        realized = await realized_stats(db, str(m["_id"]),
                                        canary.get("stage_started_at")
                                        or canary.get("started_at") or "")
        verdict = deviation_verdict(canary.get("expected") or {}, realized)
        if verdict["deviated"]:
            out.append(await rollback_canary(
                db, user_id, m,
                f"distribution deviation at {canary.get('allocation_pct')}%"
                f" allocation: {verdict['reason']}", automatic=True))
            continue
        try:
            started = datetime.fromisoformat(
                str(canary.get("stage_started_at")))
            hours = (datetime.now(timezone.utc)
                     - started).total_seconds() / 3600
        except (TypeError, ValueError):
            hours = 0.0
        if verdict["judged"] and hours >= MIN_STAGE_HOURS:
            out.append(await _advance(db, user_id, m, realized))
        else:
            out.append({"version": m["version"], "status": "canary",
                        "allocation_pct": canary.get("allocation_pct"),
                        "stage_hours": round(hours, 1),
                        "realized": realized, "hold_reason":
                        verdict["reason"] if not verdict["judged"]
                        else f"stage time {hours:.1f}h < "
                             f"{MIN_STAGE_HOURS}h"})
    return out


async def canary_status(db, user_id: str) -> list:
    out = []
    async for m in db.shadow_models.find(
            {"user_id": user_id,
             "status": {"$in": ["canary", "rolled_back"]}}).sort(
            "registered_at", -1).limit(20):
        canary = m.get("canary") or {}
        realized = await realized_stats(db, str(m["_id"]),
                                        canary.get("stage_started_at")
                                        or canary.get("started_at") or "")
        out.append({"model_id": str(m["_id"]), "version": m.get("version"),
                    "engine": m.get("engine"), "status": m.get("status"),
                    "allocation_pct": canary.get("allocation_pct"),
                    "ladder": LADDER, "stage_idx": canary.get("stage_idx"),
                    "expected": canary.get("expected"),
                    "realized_this_stage": realized,
                    "history": canary.get("history") or [],
                    "rollback_reason": m.get("rollback_reason"),
                    "started_at": canary.get("started_at")})
    return out
