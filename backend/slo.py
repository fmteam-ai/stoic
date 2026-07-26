"""iter-158 — SLOs + error budgets for the Ops Console.

Each SLO: target over a rolling window; the error budget is the allowed
failure fraction (1 - target). budget_consumed_pct >= 100 means the budget
is blown and the SLO is out of compliance.
"""
from datetime import datetime, timedelta, timezone

SLOS = {
    "api_latency": {
        "title": "API p95 latency < 800ms",
        "target_pct": 99.0,
        "window": "15m (in-process ring buffer)",
    },
    "api_availability": {
        "title": "API 5xx error rate < 1%",
        "target_pct": 99.0,
        "window": "15m (in-process ring buffer)",
    },
    "heartbeat_freshness": {
        "title": "Connected accounts with EA heartbeat < 5min",
        "target_pct": 95.0,
        "window": "current snapshot",
    },
    "deployment_success": {
        "title": "Agent deployments succeed",
        "target_pct": 95.0,
        "window": "30 days",
    },
}


def _budget(target_pct: float, good: int, total: int) -> dict:
    if total <= 0:
        return {"compliance_pct": None, "budget_consumed_pct": None,
                "good": 0, "total": 0, "status": "no_data"}
    compliance = 100.0 * good / total
    allowed_bad = total * (100.0 - target_pct) / 100.0
    bad = total - good
    consumed = (100.0 * bad / allowed_bad) if allowed_bad > 0 else (
        0.0 if bad == 0 else 999.0)
    return {"compliance_pct": round(compliance, 2),
            "budget_consumed_pct": round(min(consumed, 999.0), 1),
            "good": good, "total": total,
            "status": ("ok" if consumed < 80
                       else "at_risk" if consumed < 100 else "breached")}


async def compute_slos(db) -> dict:
    import ops_metrics
    out = {}
    api = ops_metrics.summary()
    n = api.get("count") or 0

    # api_latency — proportion of sampled requests under 800ms
    fast = 0
    if n:
        import time as _t
        cutoff = _t.time() - 900
        samples = [ms for ts, ms, _st in list(ops_metrics._SAMPLES)
                   if ts >= cutoff]
        fast = sum(1 for ms in samples if ms < 800)
        n = len(samples)
    out["api_latency"] = {**SLOS["api_latency"],
                          **_budget(SLOS["api_latency"]["target_pct"], fast, n)}

    # api_availability — non-5xx proportion
    err_pct = api.get("error_rate_pct")
    total = api.get("count") or 0
    errors = round(total * (err_pct or 0.0) / 100.0) if total else 0
    out["api_availability"] = {
        **SLOS["api_availability"],
        **_budget(SLOS["api_availability"]["target_pct"],
                  total - errors, total)}

    # heartbeat_freshness — of accounts marked connected, how many have a
    # heartbeat younger than 5 minutes right now
    now = datetime.now(timezone.utc)
    fresh_cut = (now - timedelta(minutes=5)).isoformat()
    connected = await db.accounts.count_documents({"status": "connected"})
    fresh = await db.accounts.count_documents(
        {"status": "connected", "last_heartbeat": {"$gte": fresh_cut}})
    out["heartbeat_freshness"] = {
        **SLOS["heartbeat_freshness"],
        **_budget(SLOS["heartbeat_freshness"]["target_pct"],
                  fresh, connected)}

    # deployment_success — agent-reported deploy outcomes over 30d
    month_ago = (now - timedelta(days=30)).isoformat()
    dep_total = await db.agent_deployments.count_documents(
        {"at": {"$gte": month_ago}})
    dep_ok = await db.agent_deployments.count_documents(
        {"at": {"$gte": month_ago}, "status": "success"})
    out["deployment_success"] = {
        **SLOS["deployment_success"],
        **_budget(SLOS["deployment_success"]["target_pct"],
                  dep_ok, dep_total)}
    return out
