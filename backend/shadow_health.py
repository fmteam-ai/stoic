"""Phase 1.4 — Shadow Health Score (hardened, corrections #1–#3).

#1 Unknown health data FAILS CLOSED: a missing component scores 0 when
   critical (40 when soft) instead of being skipped, is listed in
   `missing_components`, sets `fail_closed` (blocks autonomous entries in
   bot_runner, pauses promotions in promotion_gate) and drives sizing down
   through subsystem_health.
#2 Broker stability is scored PER ACCOUNT; the WORST active account drives
   the component so one healthy account can never mask a stale live one.
#3 Data freshness is scored per REQUIRED symbol feed (from the user's
   active bot configs); the worst required feed drives the component.
"""
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger("shadow-health")

THRESHOLD = 60
CRITICAL_COMPONENTS = {"data_freshness", "broker_stability",
                       "worker_health", "synchronization"}
MISSING_CRITICAL_SCORE = 0
MISSING_SOFT_SCORE = 40
STALE_AT_OR_BELOW = 15

_fc_cache: dict = {}
FC_TTL = 5 * 60


def _age_score(age_secs: float | None, fresh: float, stale: float) -> int | None:
    if age_secs is None:
        return None
    if age_secs <= fresh:
        return 100
    if age_secs >= stale:
        return 15
    return round(100 - (age_secs - fresh) / (stale - fresh) * 85)


def fail_closed_aggregate(raw: dict) -> dict:
    """Pure — fail-closed scoring over possibly-unknown components."""
    missing = sorted(k for k, v in raw.items() if v is None)
    effective = {k: (v if v is not None else
                     (MISSING_CRITICAL_SCORE if k in CRITICAL_COMPONENTS
                      else MISSING_SOFT_SCORE))
                 for k, v in raw.items()}
    overall = (round(sum(effective.values()) / len(effective))
               if effective else 0)
    stale = sorted(k for k, v in raw.items()
                   if v is not None and v <= STALE_AT_OR_BELOW)
    fail_closed = any(k in CRITICAL_COMPONENTS for k in missing)
    return {"effective": effective, "missing": missing, "stale": stale,
            "overall": overall, "fail_closed": fail_closed}


async def _data_freshness(db, user_id: str, now) -> tuple:
    """Correction #3 — per required symbol feed; worst required feed wins."""
    from pip_utils import base_symbol
    required = set()
    async for c in db.bot_configs.find({"user_id": user_id, "active": True},
                                       {"symbols": 1}):
        for s in (c.get("symbols") or []):
            required.add(base_symbol(str(s).upper()))
    feeds: dict = {}
    for sym in sorted(required):
        doc = await db.intraday_candles.find_one(
            {"user_id": user_id, "symbol": sym},
            projection={"bars": {"$slice": -1}})
        bar_t = None
        if doc and doc.get("bars"):
            bar_t = float(doc["bars"][-1].get("t") or 0)
        age = now.timestamp() - bar_t if bar_t else None
        feeds[sym] = {"age_secs": round(age) if age is not None else None,
                      "score": (_age_score(age, 20 * 60, 3 * 3600)
                                if age is not None else None)}
    if feeds:
        score = min(f["score"] if f["score"] is not None else 0
                    for f in feeds.values())
        return score, feeds
    # no active configs — fall back to the store-wide newest bar
    doc = await db.intraday_candles.find_one({}, sort=[("_id", -1)],
                                             projection={"bars": {"$slice": -1}})
    bar_t = None
    if doc and doc.get("bars"):
        bar_t = float(doc["bars"][-1].get("t") or 0)
    return _age_score(now.timestamp() - bar_t if bar_t else None,
                      20 * 60, 3 * 3600), feeds


async def _broker_stability(db, user_id: str, now) -> tuple:
    """Correction #2 — per-account scoring; the worst active account drives
    live-authority decisions. A live account that never sent a heartbeat
    scores 0 (unknown broker truth fails closed)."""
    accounts = []
    async for a in db.accounts.find(
            {"user_id": user_id, "status": {"$ne": "deleted"},
             "dormant": {"$ne": True}, "harness": {"$ne": True}},
            {"label": 1, "broker": 1, "mode": 1, "last_heartbeat": 1}):
        hb = a.get("last_heartbeat")
        if isinstance(hb, str):
            try:
                hb = datetime.fromisoformat(hb)
            except ValueError:
                hb = None
        if hb and hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        age = (now - hb).total_seconds() if hb else None
        live = str(a.get("mode") or "").lower() == "live"
        if age is None and not live:
            continue  # never-connected demo accounts don't gate authority
        accounts.append({
            "account_id": str(a["_id"]),
            "label": a.get("label") or a.get("broker"),
            "live": live,
            "age_secs": round(age) if age is not None else None,
            "score": (_age_score(age, 120, 1800)
                      if age is not None else 0)})
    stability = min((x["score"] for x in accounts), default=None)
    ages = [x["age_secs"] for x in accounts]
    if accounts and any(a is None for a in ages):
        sync = 0
    elif accounts:
        sync = _age_score(max(ages), 300, 3600)
    else:
        sync = None
    return stability, sync, accounts


async def health_score(db, user_id: str) -> dict:
    now = datetime.now(timezone.utc)
    raw: dict = {}
    details: dict = {}

    raw["data_freshness"], details["data_freshness_feeds"] = \
        await _data_freshness(db, user_id, now)

    # regime confidence + market axes
    try:
        from market_state import market_state_score
        ms = await market_state_score(db, user_id)
        conf = (ms.get("scores") or {}).get("confidence")
        raw["regime_confidence"] = (round(float(conf))
                                    if conf is not None
                                    else ms.get("market_health"))
    except Exception as e:  # noqa: BLE001
        logger.warning("market state axis failed: %s", e)
        raw["regime_confidence"] = None

    # calibration quality — 100 − 4×MAE (5pt MAE → 80)
    try:
        from calibration import compute_calibration
        table = await compute_calibration(db, user_id, days=90)
        tot = err = 0.0
        for ent in table.values():
            for b in ent["buckets"]:
                tot += b["n"]
                err += abs(b["gap"]) * b["n"]
        raw["calibration_quality"] = (
            max(0, round(100 - (err / tot) * 4)) if tot else None)
    except Exception as e:  # noqa: BLE001
        logger.warning("calibration axis failed: %s", e)
        raw["calibration_quality"] = None

    # execution quality — user's own accounts
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
    raw["execution_quality"] = (round(sum(scores) / len(scores))
                                if scores else None)

    raw["broker_stability"], raw["synchronization"], \
        details["broker_stability_accounts"] = \
        await _broker_stability(db, user_id, now)

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
    raw["worker_health"] = round(alive / total * 100) if total else None

    agg = fail_closed_aggregate(raw)
    return {"components": agg["effective"], "raw_components": raw,
            "missing_components": agg["missing"],
            "stale_components": agg["stale"],
            "fail_closed": agg["fail_closed"],
            "details": details,
            "overall": agg["overall"], "threshold": THRESHOLD,
            "promotions_paused": (agg["overall"] < THRESHOLD
                                  or agg["fail_closed"]),
            "at": now.isoformat()}


async def fail_closed_status(db, user_id: str) -> dict:
    """5-min-cached fail-closed probe for the live entry path."""
    now = time.time()
    cached = _fc_cache.get(user_id)
    if cached and cached[0] > now:
        return cached[1]
    hs = await health_score(db, user_id)
    out = {"fail_closed": hs["fail_closed"],
           "missing": hs["missing_components"],
           "overall": hs["overall"]}
    _fc_cache[user_id] = (now + FC_TTL, out)
    return out
