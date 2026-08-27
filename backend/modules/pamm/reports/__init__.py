"""PAMM reporting — performance summaries from broker-authoritative NAV."""


async def performance_summary(db, program_id: str) -> dict:
    navs = [n async for n in db.pamm_nav_snapshots
            .find({"program_id": program_id}, {"_id": 0})
            .sort("at", 1).limit(2000)]
    if not navs:
        return {"program_id": program_id, "points": 0}
    values = [n["nav"] for n in navs]
    first, last, peak = values[0], values[-1], values[0]
    max_dd = 0.0
    for v in values:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak * 100 if peak else 0)
    return {"program_id": program_id, "points": len(values),
            "first_nav": first, "current_nav": last,
            "total_return_pct": round((last - first) / first * 100, 3)
            if first else 0.0,
            "max_drawdown_pct": round(max_dd, 3),
            "from": navs[0]["at"], "to": navs[-1]["at"]}
