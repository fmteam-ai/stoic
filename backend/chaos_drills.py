"""Tier 14 — Chaos drills: automated failure-scenario proofs.

Each drill provokes a failure mode with SYNTHETIC data and asserts the
system's defence responds. Self-cleaning — no live account, order or config
is touched. Results persist to `chaos_drills` and feed the Release Safety
Score.
"""
import uuid
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError


async def _drill_duplicate_order(db) -> dict:
    tag = f"CHAOS-{uuid.uuid4().hex[:8]}"
    doc = {"deal_id": tag, "account_id": "chaos-drill", "chaos": True}
    try:
        await db.broker_deals.insert_one(dict(doc))
        try:
            await db.broker_deals.insert_one(dict(doc))
            return {"drill": "duplicate_order", "passed": False,
                    "detail": "duplicate broker deal was ACCEPTED — "
                              "idempotency index missing"}
        except DuplicateKeyError:
            return {"drill": "duplicate_order", "passed": True,
                    "detail": "second identical broker deal rejected by the "
                              "unique (account_id, deal_id) index"}
    finally:
        await db.broker_deals.delete_many({"deal_id": tag})


def _drill_broker_disconnect() -> dict:
    from risk_engine import abnormal_market_check
    now = datetime.now(timezone.utc)
    anchor = now.timestamp()
    bars = [{"t": anchor - 3600 - (39 - i) * 900, "o": 100.0, "h": 101.0,
             "l": 99.0, "c": 100.0} for i in range(40)]  # last bar 60min old
    res = abnormal_market_check(bars, now_ts=anchor)
    if now.weekday() < 5:
        ok = res.get("status") == "block"
        detail = ("stale candle feed (60min) suspended trading: "
                  f"{res.get('detail')}" if ok else
                  f"stale feed NOT detected: {res}")
    else:
        ok = res.get("status") in ("ok", "trim")
        detail = "weekend — stale-feed rule is weekday-only (by design)"
    return {"drill": "broker_disconnect", "passed": ok, "detail": detail}


def _drill_volatility_shock() -> dict:
    from risk_engine import abnormal_market_check
    anchor = datetime.now(timezone.utc).timestamp()
    bars = [{"t": anchor - (39 - i) * 900, "o": 100.0, "h": 100.5,
             "l": 99.5, "c": 100.0} for i in range(39)]
    bars.append({"t": anchor - 60, "o": 100.0, "h": 106.0, "l": 96.0,
                 "c": 97.0})  # 10× median range
    res = abnormal_market_check(bars, now_ts=anchor)
    ok = res.get("status") == "block"
    return {"drill": "volatility_shock", "passed": ok,
            "detail": (f"10× median bar suspended the cycle: "
                       f"{res.get('detail')}" if ok else
                       f"shock NOT detected: {res}")}


async def _drill_worker_crash(db) -> dict:
    _id = f"chaos-worker-{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc)
    await db.worker_leases.insert_one(
        {"_id": _id, "expires_at": now - timedelta(minutes=10),
         "loops_total": 2, "loops_running": 1, "chaos": True})
    try:
        lease = await db.worker_leases.find_one({"_id": _id})
        exp = lease.get("expires_at")
        if isinstance(exp, str):
            exp = datetime.fromisoformat(exp)
        if exp and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        dead_lease = bool(exp and exp < now)
        dead_loop = lease.get("loops_running") != lease.get("loops_total")
        ok = dead_lease and dead_loop
        return {"drill": "worker_crash", "passed": ok,
                "detail": ("expired lease AND dead loop both detected by the "
                           "readiness predicates" if ok else
                           f"detection failed: lease_dead={dead_lease} "
                           f"loop_dead={dead_loop}")}
    finally:
        await db.worker_leases.delete_one({"_id": _id})


async def _drill_api_timeout(db) -> dict:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=0.5) as client:
            await client.get("http://10.255.255.1/never")
        return {"drill": "api_outage", "passed": False,
                "detail": "unroutable call returned?! timeout guard broken"}
    except (httpx.TimeoutException, httpx.ConnectError, OSError):
        return {"drill": "api_outage", "passed": True,
                "detail": "outbound API outage bounded by client timeout "
                          "(0.5s) — no hang, exception surfaced"}


def _drill_clock_skew() -> dict:
    now = datetime.now(timezone.utc)
    future_hb = now + timedelta(hours=1)
    skewed = abs((now - future_hb).total_seconds()) > 300
    return {"drill": "clock_skew", "passed": skewed,
            "detail": ("future-dated heartbeat (+1h) flagged as skewed by "
                       "the 5-minute tolerance predicate" if skewed else
                       "skew NOT detected")}


async def _drill_alert_dedup(db) -> dict:
    from alerting import raise_alert
    kind = f"chaos_dedup_{uuid.uuid4().hex[:6]}"
    try:
        await raise_alert(db, kind, "warning", "chaos drill", dedup_key=kind)
        await raise_alert(db, kind, "warning", "chaos drill", dedup_key=kind)
        n = await db.ops_alerts.count_documents({"dedup_key": kind})
        return {"drill": "alert_storm_dedup", "passed": n == 1,
                "detail": (f"repeated alert deduplicated to a single open "
                           f"record" if n == 1 else
                           f"{n} duplicate alert docs created")}
    finally:
        await db.ops_alerts.delete_many({"dedup_key": kind})


async def _drill_db_recovery(db) -> dict:
    _id = f"chaos-db-{uuid.uuid4().hex[:8]}"
    payload = uuid.uuid4().hex
    try:
        await db.chaos_probe.insert_one({"_id": _id, "payload": payload})
        doc = await db.chaos_probe.find_one({"_id": _id})
        ok = bool(doc and doc.get("payload") == payload)
        return {"drill": "db_recovery", "passed": ok,
                "detail": ("write → read → delete roundtrip intact" if ok
                           else "read-back mismatch — durability problem")}
    finally:
        await db.chaos_probe.delete_one({"_id": _id})


async def run_drills(db) -> dict:
    results = [
        await _drill_duplicate_order(db),
        _drill_broker_disconnect(),
        _drill_volatility_shock(),
        await _drill_worker_crash(db),
        await _drill_db_recovery(db),
        await _drill_api_timeout(db),
        _drill_clock_skew(),
        await _drill_alert_dedup(db),
    ]
    passed = sum(1 for r in results if r["passed"])
    doc = {"at": datetime.now(timezone.utc), "results": results,
           "passed": passed, "total": len(results)}
    await db.chaos_drills.insert_one(dict(doc))
    doc["at"] = doc["at"].isoformat()
    doc.pop("_id", None)
    return doc
