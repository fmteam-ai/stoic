"""Public trust statistics — audit P1-1 / P1-2 corrections.

Populations are DEFINED and VERSIONED (`POPULATION_VERSION`); every payload
publishes environment breakdown, exclusions, period, as-of and
reconciliation status. Nothing here is a marketing estimate:

* `verified_active_accounts` — unique non-deleted, non-synthetic accounts
  with `trading_enabled: True` (explicit consent to the safety policy),
  verified broker identity and a heartbeat within 30 days.
* `execution_intents_blocked` — unique execution intents refused at the
  canonical authority/execution choke point (pre-dispatch `rejected`),
  QA/synthetic excluded. HOLD verdicts are NOT counted (a HOLD is not an
  attempted trade).
* `availability` — from independent edge probes (`edge_probes`, written by
  the external prober through /api/public/edge-probe) using the published
  SLI: successful eligible requests / total eligible requests. Exposed ONLY
  once a complete, reconciled trailing 30-day window exists; otherwise
  `null` with `status: "window_incomplete"`. The old ops_soak_samples ratio
  is published as `ops_monitoring_coverage_pct` (internal), never as uptime.
"""
import os
from datetime import datetime, timedelta, timezone

from broker_env import broker_environment
from synthetic_data import is_synthetic_account

POPULATION_VERSION = "trust-stats/v2"
SLI = {"name": "edge_availability_30d",
       "definition": "successful eligible requests / total eligible requests",
       "eligible_endpoints": ["/api/health", "/", "/welcome", "/login", "/dashboard"],
       "success": "HTTP 200 within 10s from an external region",
       "sample_rule": "one server-bucketed sample per (region, endpoint, minute); region derived from the prober token; duplicates ignored",
       "interval_seconds": 60, "window_days": 30, "min_coverage_pct_per_series": 95.0,
       "exclusions": ["prober-side network failures (prober_error=true)",
                      "endpoints outside the allowlist (rejected at ingestion)"]}


def _iso(dt: datetime) -> str:
    return dt.isoformat()


async def verified_active_accounts(db, now: datetime) -> dict:
    since = _iso(now - timedelta(days=30))
    rows = await db.accounts.find(
        {"status": {"$ne": "deleted"}, "trading_enabled": True,
         "verified_identity.account_number": {"$exists": True},
         "last_heartbeat": {"$gte": since}},
        {"label": 1, "user_id": 1, "broker_environment": 1, "mode": 1, "account_type": 1,
         "broker_server": 1, "server": 1, "verified_identity": 1, "synthetic": 1}
    ).to_list(length=5000)
    seen, env = set(), {"LIVE": 0, "DEMO": 0, "PAPER": 0}
    for a in rows:
        if is_synthetic_account(a):
            continue
        key = (a.get("verified_identity") or {}).get("account_number") or str(a["_id"])
        if key in seen:
            continue
        seen.add(key)
        env[broker_environment(a)] = env.get(broker_environment(a), 0) + 1
    return {"count": len(seen), "environment": env,
            "definition": "unique non-deleted accounts · trading_enabled=true (explicit safety-policy consent) · "
                          "verified broker identity · heartbeat ≤30d · deduplicated on verified account number",
            "exclusions": ["deleted", "trading_enabled missing/false", "unverified identity",
                           "no heartbeat in 30d", "synthetic/QA labels", "duplicate broker accounts"]}


async def execution_intents_blocked(db, now: datetime) -> dict:
    since = _iso(now - timedelta(days=30))
    pipeline = [
        {"$match": {"status": "rejected", "created_at": {"$gte": since},
                    "kind": {"$in": ["open_trade", "open", "new_exposure", "entry"]}}},
        {"$group": {"_id": "$intent_id", "actor": {"$first": "$actor"},
                    "account_id": {"$first": "$account_id"}}}]
    rows = await db.execution_intents.aggregate(pipeline).to_list(length=100000)
    if not rows:
        return {"count": 0, "period_days": 30, "definition": _BLOCKED_DEF, "exclusions": _BLOCKED_EXCL}
    acct_ids = {r.get("account_id") for r in rows if r.get("account_id")}
    from bson import ObjectId
    oids = []
    for a in acct_ids:
        try:
            oids.append(ObjectId(a))
        except Exception:  # noqa: BLE001
            pass
    accs = {str(a["_id"]): a for a in await db.accounts.find(
        {"_id": {"$in": oids}}, {"label": 1, "user_id": 1, "synthetic": 1}).to_list(length=len(oids))} if oids else {}
    n = 0
    for r in rows:
        acc = accs.get(r.get("account_id"))
        if acc is not None and is_synthetic_account(acc):
            continue
        if str(r.get("actor") or "").startswith(("test_", "qa_", "synthetic_", "chaos_")):
            continue
        n += 1
    return {"count": n, "period_days": 30, "definition": _BLOCKED_DEF, "exclusions": _BLOCKED_EXCL}


_BLOCKED_DEF = ("unique new-exposure execution intents refused pre-dispatch at the canonical "
                "authority/execution choke point (status=rejected) in the trailing 30 days")
_BLOCKED_EXCL = ["HOLD verdicts (no trade attempted)", "synthetic/QA accounts and users",
                 "close/reduce intents", "duplicates by intent_id"]


async def availability_30d(db, now: datetime) -> dict:
    from edge_probes import ALLOWED_ENDPOINTS, configured_regions, coverage
    start = now - timedelta(days=30)
    out = {"sli": {**SLI, "eligible_endpoints": list(ALLOWED_ENDPOINTS), "configured_regions": configured_regions()},
           "value_pct": None, "status": "no_probes", "window_start": _iso(start), "window_end": _iso(now),
           "reconciled": False, "per_region": [], "gaps": []}
    if not configured_regions():
        out["status"] = "no_regions_configured"
        return out
    first = await db.edge_probes.find_one({}, sort=[("minute", 1)], projection={"minute": 1})
    if not first:
        return out
    first_at = first["minute"] if first["minute"].tzinfo else first["minute"].replace(tzinfo=timezone.utc)
    if first_at > start:
        out["status"] = "window_incomplete"
        out["window_covered_days"] = round((now - first_at).total_seconds() / 86400, 1)
        return out
    cov = await coverage(db, start, now)
    out["per_region"] = cov["series"]
    out["gaps"] = cov["gaps"]
    if cov["gaps"]:
        out["status"] = "window_incomplete"
        return out
    total = sum(s["samples"] for s in cov["series"])
    ok = sum(s["ok"] for s in cov["series"])
    out.update(value_pct=round(ok * 100.0 / max(1, total), 3), status="reconciled", reconciled=True,
               probes=total, successful=ok, regions=cov["configured_regions"])
    return out


async def ops_monitoring_coverage(db, now: datetime) -> dict:
    first = await db.ops_soak_samples.find_one({}, sort=[("at", 1)], projection={"at": 1})
    if not first or not first.get("at"):
        return {"pct": None, "definition": _COV_DEF}
    start = first["at"]
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = max(start, now - timedelta(days=30))
    expected = max(1, int((now - start).total_seconds() // 1800))
    got = await db.ops_soak_samples.count_documents({"at": {"$gte": start.replace(tzinfo=None)}})
    return {"pct": round(min(100.0, got * 100.0 / expected), 2), "definition": _COV_DEF}


_COV_DEF = ("share of expected 30-minute ops soak samples that were recorded — measures the "
            "internal sampler, NOT public availability; never shown as uptime")


def legal_approved() -> bool:
    return os.environ.get("TRUST_STATS_LEGAL_APPROVED", "").lower() == "true"


async def build_trust_stats(db) -> dict:
    now = datetime.now(timezone.utc)
    acc = await verified_active_accounts(db, now)
    blk = await execution_intents_blocked(db, now)
    avail = await availability_30d(db, now)
    cov = await ops_monitoring_coverage(db, now)
    return {
        "population_version": POPULATION_VERSION,
        "verified_active_accounts": acc,
        "execution_intents_blocked": blk,
        "availability": avail,
        "ops_monitoring_coverage_pct": cov["pct"],
        "ops_monitoring_coverage_definition": cov["definition"],
        # legacy keys kept for older clients — SAME defined populations, never the raw counts
        "accounts_protected": acc["count"],
        "signals_vetoed": blk["count"],
        "uptime_30d_pct": avail["value_pct"],
        "as_of": _iso(now), "period": {"days": 30, "start": _iso(now - timedelta(days=30)), "end": _iso(now)},
        "source": "platform_db_aggregate", "environment_breakdown": acc["environment"],
        "reconciliation": "reconciled" if avail["reconciled"] else "availability_window_incomplete",
        "legal_review": "approved" if legal_approved() else "pending — section withheld from the public page until TRUST_STATS_LEGAL_APPROVED=true",
        "published": legal_approved(),
        "context": "demo/paper environments included where shown; figures describe platform activity, not profitability or customer capital protection",
        "ttl_seconds": 900,
    }
