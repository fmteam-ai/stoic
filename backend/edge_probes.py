"""Edge availability probes — hardened ingestion + per-region SLI (audit P1-3).

Contract: one valid sample per (region, endpoint, minute). Region comes from
the prober's own token (EDGE_PROBE_TOKENS="fra:tok1,iad:tok2"); the endpoint
must be allowlisted; `eligible` is computed here (never trusted); server time
is bucketed to the minute and written with an upsert on a unique index, so
duplicate submissions can never inflate the count. Coverage is required PER
configured region before any percentage is published.
"""
import hmac
import os
from datetime import datetime, timezone

from fastapi import HTTPException

ALLOWED_ENDPOINTS = ("/api/health", "/")
INTERVAL_S = 60


def probe_tokens() -> dict:
    """region → token, parsed from EDGE_PROBE_TOKENS (fallback: legacy single EDGE_PROBE_TOKEN → region 'default')."""
    raw = os.environ.get("EDGE_PROBE_TOKENS", "")
    out = {}
    for part in raw.split(","):
        if ":" in part:
            r, t = part.split(":", 1)
            r, t = r.strip().lower()[:24], t.strip()
            if r and len(t) >= 16:
                out[r] = t
    legacy = os.environ.get("EDGE_PROBE_TOKEN")
    if not out and legacy:
        out["default"] = legacy
    return out


def configured_regions() -> list:
    return sorted(probe_tokens().keys())


def region_for_token(got: str) -> str | None:
    for region, tok in probe_tokens().items():
        if hmac.compare_digest(got or "", tok):
            return region
    return None


async def ensure_indexes(db):
    await db.edge_probes.create_index([("region", 1), ("endpoint", 1), ("minute", 1)],
                                      unique=True, name="uniq_region_endpoint_minute")
    await db.edge_probes.create_index([("at", 1)], name="at")


async def ingest_probe(db, token: str, body: dict) -> dict:
    region = region_for_token(token)
    if region is None:
        raise HTTPException(status_code=401, detail="edge probe token invalid")
    endpoint = str(body.get("endpoint") or "").strip()[:80]
    now = datetime.now(timezone.utc)
    minute = now.replace(second=0, microsecond=0)
    if endpoint not in ALLOWED_ENDPOINTS:
        await db.edge_probe_rejects.insert_one({"at": now, "region": region, "endpoint": endpoint, "reason": "endpoint_not_allowlisted"})
        raise HTTPException(status_code=422, detail={"code": "endpoint_not_allowlisted", "allowed": list(ALLOWED_ENDPOINTS)})
    await ensure_indexes(db)
    status_code = body.get("status_code")
    latency = body.get("latency_ms")
    prober_error = bool(body.get("prober_error", False))
    ok = bool(body.get("ok")) and (status_code in (None, 200)) and not prober_error
    doc = {"at": now, "minute": minute, "region": region, "endpoint": endpoint, "ok": ok,
           "status_code": status_code if isinstance(status_code, int) else None,
           "latency_ms": latency if isinstance(latency, (int, float)) else None,
           "eligible": not prober_error, "prober_error": prober_error}
    res = await db.edge_probes.update_one({"region": region, "endpoint": endpoint, "minute": minute},
                                          {"$setOnInsert": doc, "$inc": {"submissions": 1}}, upsert=True)
    duplicate = res.upserted_id is None
    if duplicate:
        await db.edge_probe_rejects.insert_one({"at": now, "region": region, "endpoint": endpoint, "reason": "duplicate_minute"})
        dups = await db.edge_probe_rejects.count_documents({"reason": "duplicate_minute", "at": {"$gte": now.replace(hour=0, minute=0, second=0, microsecond=0)}})
        if dups in (10, 100, 1000):
            await db.ops_alerts.insert_one({"severity": "warning", "kind": "edge_probe_duplicates",
                                            "message": f"{dups} duplicate edge-probe submissions today (region {region})",
                                            "at": now.isoformat(), "acknowledged": False, "synthetic": False})
    return {"recorded": not duplicate, "duplicate": duplicate, "region": region, "endpoint": endpoint,
            "minute": minute.isoformat(), "eligible": doc["eligible"]}


async def coverage(db, start: datetime, now: datetime) -> dict:
    """Per-region/endpoint sample coverage over the window (for the SLI gate)."""
    expected_per_series = int((now - start).total_seconds() // INTERVAL_S)
    pipeline = [{"$match": {"minute": {"$gte": start}, "eligible": True}},
                {"$group": {"_id": {"region": "$region", "endpoint": "$endpoint"},
                            "n": {"$sum": 1}, "ok": {"$sum": {"$cond": ["$ok", 1, 0]}}}}]
    rows = await db.edge_probes.aggregate(pipeline).to_list(length=500)
    series = {}
    for r in rows:
        key = f"{r['_id']['region']}|{r['_id']['endpoint']}"
        series[key] = {"region": r["_id"]["region"], "endpoint": r["_id"]["endpoint"], "samples": r["n"],
                       "ok": r["ok"], "expected": expected_per_series,
                       "coverage_pct": round(r["n"] * 100.0 / max(1, expected_per_series), 2)}
    gaps = []
    for region in configured_regions():
        for ep in ALLOWED_ENDPOINTS:
            s = series.get(f"{region}|{ep}")
            if not s or s["coverage_pct"] < 95.0:
                gaps.append({"region": region, "endpoint": ep, "coverage_pct": (s or {}).get("coverage_pct", 0.0)})
    return {"series": list(series.values()), "gaps": gaps, "expected_per_series": expected_per_series,
            "configured_regions": configured_regions()}
