"""Soak Memory Watch (P1 backlog).

Tracks worker RSS across the soak from `ops_soak_samples` (10-min sampler),
folds it into daily checkpoints and raises an ops alert when memory keeps
climbing between checkpoints — a slow leak must be visible on day 3, not on
day 14 when the pod is OOM-killed.
"""
import os
from datetime import datetime, timedelta, timezone

WATCH_GROWTH_PCT = 10.0      # vs. baseline (first day median)
ALERT_GROWTH_PCT = 25.0
WATCH_SLOPE_MB_DAY = 5.0     # least-squares slope over daily medians
ALERT_SLOPE_MB_DAY = 15.0
CLIMB_STEP_PCT = 3.0         # a day counts as "climbing" above this delta
ALERT_CONSECUTIVE_CLIMBS = 3
MIN_SAMPLES = 6


def _median(xs: list) -> float:
    s = sorted(xs)
    n = len(s)
    return float(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)


def _to_dt(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _slope(points: list) -> float:
    """Least-squares slope in MB/day for [(day_index, mb)]."""
    n = len(points)
    if n < 2:
        return 0.0
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    den = sum((p[0] - mx) ** 2 for p in points)
    if den == 0:
        return 0.0
    return sum((p[0] - mx) * (p[1] - my) for p in points) / den


def _instance(s: dict) -> str:
    return f"{s.get('service') or 'api'}@{s.get('host') or '?'}:{s.get('pid') or '?'}"


def instance_coverage(samples: list, expected_services: set | None = None) -> dict:
    """Per-instance series + which expected services have NO samples (r14 P2-01)."""
    seen: dict = {}
    for s in samples:
        if s.get("rss_mb") is None:
            continue
        seen.setdefault(_instance(s), {"service": s.get("service") or "api", "samples": 0,
                                       "last_mb": None, "build": s.get("build")})
        seen[_instance(s)]["samples"] += 1
        seen[_instance(s)]["last_mb"] = float(s["rss_mb"])
    expected = expected_services or set(
        x.strip() for x in (os.environ.get("SOAK_EXPECTED_SERVICES") or "api").split(",") if x.strip())
    present = {v["service"] for v in seen.values()}
    return {"instances": seen, "expected_services": sorted(expected),
            "missing_services": sorted(expected - present),
            "workers_covered": not (expected - present) and len(expected) > 1}


def memory_trend(samples: list, started_at=None) -> dict:
    """Pure. `samples` = [{at, rss_mb, service?, host?, pid?}] oldest→newest.
    Multi-instance samples are folded per day by MAX (the leaking replica
    dominates) while per-instance coverage is reported alongside."""
    pts = [(_to_dt(s.get("at")), float(s["rss_mb"])) for s in samples
           if s.get("rss_mb") is not None and _to_dt(s.get("at")) is not None]
    cov = instance_coverage(samples)
    if len(pts) < MIN_SAMPLES:
        return {"verdict": "INSUFFICIENT", "samples": len(pts), "coverage": cov,
                "detail": f"{len(pts)}/{MIN_SAMPLES} memory samples — keep the soak running"}
    t0 = _to_dt(started_at) or pts[0][0]
    by_day: dict = {}
    for at, mb in pts:
        by_day.setdefault(int((at - t0).total_seconds() // 86400) + 1, []).append(mb)
    daily = [{"day": d, "median_mb": round(_median(v), 1), "max_mb": round(max(v), 1),
              "samples": len(v)} for d, v in sorted(by_day.items())]
    baseline = daily[0]["median_mb"]
    latest = daily[-1]["median_mb"]
    growth_pct = round((latest - baseline) / baseline * 100, 1) if baseline else 0.0
    slope = round(_slope([(d["day"], d["median_mb"]) for d in daily]), 2)
    climbs = 0
    for prev, cur in zip(daily, daily[1:]):
        step = (cur["median_mb"] - prev["median_mb"]) / prev["median_mb"] * 100 if prev["median_mb"] else 0
        climbs = climbs + 1 if step > CLIMB_STEP_PCT else 0
    if cov["missing_services"]:
        verdict = "ALERT"
    elif (growth_pct >= ALERT_GROWTH_PCT or slope >= ALERT_SLOPE_MB_DAY
            or climbs >= ALERT_CONSECUTIVE_CLIMBS):
        verdict = "ALERT"
    elif growth_pct >= WATCH_GROWTH_PCT or slope >= WATCH_SLOPE_MB_DAY or climbs >= 2:
        verdict = "WATCH"
    else:
        verdict = "OK"
    scope = "fleet RSS" if cov["workers_covered"] else "API process RSS"
    detail = (f"{scope} {latest:.0f} MB · {growth_pct:+.1f}% vs day-1 baseline {baseline:.0f} MB · "
              f"{slope:+.1f} MB/day · {climbs} consecutive climbing day(s)"
              + (f" · MISSING telemetry: {', '.join(cov['missing_services'])}" if cov["missing_services"] else ""))
    return {"verdict": verdict, "samples": len(pts), "baseline_mb": baseline,
            "latest_mb": latest, "current_mb": round(pts[-1][1], 1),
            "peak_mb": round(max(p[1] for p in pts), 1),
            "growth_pct": growth_pct, "slope_mb_per_day": slope,
            "consecutive_climbs": climbs, "daily": daily, "detail": detail,
            "scope": scope, "coverage": cov,
            "thresholds": {"watch_growth_pct": WATCH_GROWTH_PCT,
                           "alert_growth_pct": ALERT_GROWTH_PCT,
                           "watch_slope_mb_day": WATCH_SLOPE_MB_DAY,
                           "alert_slope_mb_day": ALERT_SLOPE_MB_DAY,
                           "alert_consecutive_climbs": ALERT_CONSECUTIVE_CLIMBS}}


async def campaign_trend(db, campaign: dict | None = None, days: int = 45) -> dict:
    campaign = campaign or await db.soak_campaigns.find_one({"status": "RUNNING"})
    started = (campaign or {}).get("started_at")
    since = _to_dt(started) or (datetime.now(timezone.utc) - timedelta(days=days))
    samples = await db.ops_soak_samples.find(
        {"at": {"$gte": since}}, {"at": 1, "rss_mb": 1}).sort("at", 1).to_list(20000)
    out = memory_trend(samples, started)
    out["campaign_id"] = (campaign or {}).get("campaign_id")
    return out


async def sweep(db) -> dict:
    """Called by the soak sampler after each sample: alert when memory climbs."""
    campaign = await db.soak_campaigns.find_one({"status": "RUNNING"})
    if not campaign:
        return {"skipped": "no_running_campaign"}
    trend = await campaign_trend(db, campaign)
    if trend["verdict"] not in ("WATCH", "ALERT"):
        return {"verdict": trend["verdict"], "alerted": False}
    from alerting import raise_alert
    day = trend["daily"][-1]["day"] if trend.get("daily") else 0
    severity = "critical" if trend["verdict"] == "ALERT" else "warning"
    await raise_alert(
        db, "soak_memory_growth", severity,
        f"Soak memory {trend['verdict']}: {trend['detail']}",
        dedup_key=f"soak_mem_{campaign['campaign_id']}_{trend['verdict']}_{day}",
        meta={"campaign_id": campaign["campaign_id"], "growth_pct": trend["growth_pct"],
              "slope_mb_per_day": trend["slope_mb_per_day"], "latest_mb": trend["latest_mb"]})
    return {"verdict": trend["verdict"], "alerted": True}
