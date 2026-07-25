"""Phase 1.4 — Shadow Health Score.

Live 0–100 score composed from: data freshness, regime confidence,
calibration quality, execution quality, broker stability, worker health and
synchronization. Mode promotions toward live execution are PAUSED
automatically when the score drops below THRESHOLD (wired into
operational_modes.promotion_gate).
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger("shadow-health")

THRESHOLD = 60


def _age_score(age_secs: float | None, fresh: float, stale: float) -> int | None:
    if age_secs is None:
        return None
    if age_secs <= fresh:
        return 100
    if age_secs >= stale:
        return 15
    return round(100 - (age_secs - fresh) / (stale - fresh) * 85)


async def health_score(db, user_id: str) -> dict:
    now = datetime.now(timezone.utc)
    comps: dict = {}

    # data freshness — newest M15 bar across the candle store
    doc = await db.intraday_candles.find_one({}, sort=[("_id", -1)],
                                             projection={"bars": {"$slice": -1}})
    bar_t = None
    if doc and doc.get("bars"):
        bar_t = float(doc["bars"][-1].get("t") or 0)
    comps["data_freshness"] = _age_score(
        now.timestamp() - bar_t if bar_t else None, 20 * 60, 3 * 3600)

    # regime confidence + market axes
    try:
        from market_state import market_state_score
        ms = await market_state_score(db, user_id)
        conf = (ms.get("scores") or {}).get("confidence")
        comps["regime_confidence"] = (round(float(conf))
                                      if conf is not None
                                      else ms.get("market_health"))
    except Exception as e:  # noqa: BLE001
        logger.warning("market state axis failed: %s", e)
        comps["regime_confidence"] = None

    # calibration quality — 100 − 4×MAE (5pt MAE → 80)
    try:
        from calibration import compute_calibration
        table = await compute_calibration(db, user_id, days=90)
        tot = err = 0.0
        for ent in table.values():
            for b in ent["buckets"]:
                tot += b["n"]
                err += abs(b["gap"]) * b["n"]
        comps["calibration_quality"] = (
            max(0, round(100 - (err / tot) * 4)) if tot else None)
    except Exception as e:  # noqa: BLE001
        logger.warning("calibration axis failed: %s", e)
        comps["calibration_quality"] = None

    # execution quality + broker stability — user's own accounts
    own = [str(a["_id"]) async for a in
           db.accounts.find({"user_id": user_id}, {"_id": 1})]
    scores, seen = [], set()
    async for s in db.broker_intel_scores.find(
            {"account_id": {"$in": own}}).sort("at", -1).limit(30):
        if s.get("account_id") in seen:
            continue
        seen.add(s.get("account_id"))
        if s.get("score") is not None:
            scores.append(float(s["score"]))
    comps["execution_quality"] = (round(sum(scores) / len(scores))
                                  if scores else None)

    hb_ages = []
    async for a in db.accounts.find(
            {"user_id": user_id, "status": {"$ne": "deleted"},
             "last_heartbeat": {"$ne": None}},
            {"last_heartbeat": 1}):
        hb = a.get("last_heartbeat")
        if isinstance(hb, str):
            try:
                hb = datetime.fromisoformat(hb)
            except ValueError:
                continue
        if hb and hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        if hb:
            hb_ages.append((now - hb).total_seconds())
    comps["broker_stability"] = _age_score(
        min(hb_ages) if hb_ages else None, 120, 1800)
    comps["synchronization"] = _age_score(
        max(hb_ages) if hb_ages else None, 300, 3600)

    # worker health — live leases
    total = alive = 0
    async for w in db.worker_leases.find({}):
        exp = w.get("expires_at")
        if isinstance(exp, str):
            try:
                exp = datetime.fromisoformat(exp)
            except ValueError:
                exp = None
        if exp and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        total += 1
        if exp and exp > now:
            alive += 1
    comps["worker_health"] = round(alive / total * 100) if total else None

    known = [v for v in comps.values() if v is not None]
    overall = round(sum(known) / len(known)) if known else None
    return {"components": comps, "overall": overall,
            "threshold": THRESHOLD,
            "promotions_paused": (overall is not None
                                  and overall < THRESHOLD),
            "at": now.isoformat()}
