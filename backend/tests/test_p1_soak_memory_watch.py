"""Soak Memory Watch (P1 backlog) — trend math, alert sweep, checkpoint fold."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _samples(daily_mb: list, per_day: int = 6) -> list:
    out = []
    for d, mb in enumerate(daily_mb):
        for i in range(per_day):
            out.append({"at": T0 + timedelta(days=d, hours=i * 4), "rss_mb": mb + (i % 2) * 0.5})
    return out


def test_flat_memory_is_ok():
    from soak_memory_watch import memory_trend
    t = memory_trend(_samples([400, 401, 399, 402, 400]), T0)
    assert t["verdict"] == "OK"
    assert t["baseline_mb"] == pytest.approx(400.5, abs=0.6)
    assert abs(t["slope_mb_per_day"]) < 1
    assert len(t["daily"]) == 5 and t["daily"][0]["day"] == 1


def test_slow_climb_is_watch_then_alert():
    from soak_memory_watch import memory_trend
    watch = memory_trend(_samples([400, 425, 450]), T0)   # +12.5%, 25 MB/day → ALERT slope
    assert watch["verdict"] == "ALERT"
    mild = memory_trend(_samples([400, 420, 445]), T0)    # +11%, slope 22 → ALERT
    assert mild["verdict"] == "ALERT"
    gentle = memory_trend(_samples([400, 404, 408, 412, 416, 420, 424, 428, 432, 436, 440, 444]), T0)
    assert gentle["verdict"] == "WATCH"                   # +11% over 12 days, 4 MB/day, 1% steps → WATCH not ALERT
    step = memory_trend(_samples([400, 416, 432, 449]), T0)  # three +4% climbing days
    assert step["consecutive_climbs"] == 3 and step["verdict"] == "ALERT"
    watch_only = memory_trend(_samples([400, 400, 400, 400, 400, 400, 400, 400, 400, 400, 442]), T0)
    assert watch_only["verdict"] == "WATCH"


def test_insufficient_samples():
    from soak_memory_watch import memory_trend
    t = memory_trend(_samples([400], per_day=3), T0)
    assert t["verdict"] == "INSUFFICIENT"


def test_sweep_raises_deduped_alert(request):
    from database import get_db
    from soak_memory_watch import sweep
    from bson import ObjectId
    db = get_db()
    cid = f"memtest-{ObjectId()}"
    started = datetime.now(timezone.utc) - timedelta(days=3, hours=1)

    def _cleanup():
        _run(db.soak_campaigns.delete_many({"campaign_id": cid}))
        _run(db.ops_soak_samples.delete_many({"_memtest": cid}))
        _run(db.ops_alerts.delete_many({"meta.campaign_id": cid}))
    request.addfinalizer(_cleanup)

    async def _t():
        # park any other RUNNING campaign so the sweep sees ours
        others = await db.soak_campaigns.find({"status": "RUNNING"}).to_list(10)
        await db.soak_campaigns.update_many({"status": "RUNNING"}, {"$set": {"status": "PAUSED_MEMTEST"}})
        try:
            await db.soak_campaigns.insert_one({"campaign_id": cid, "status": "RUNNING",
                                                "started_at": started.isoformat()})
            await db.ops_soak_samples.delete_many({"at": {"$gte": started}})
            docs = []
            for d, mb in enumerate([400, 430, 470, 520]):
                for i in range(6):
                    docs.append({"at": started + timedelta(days=d, hours=i * 3 + 1),
                                 "rss_mb": mb, "_memtest": cid})
            await db.ops_soak_samples.insert_many(docs)
            r1 = await sweep(db)
            assert r1["verdict"] == "ALERT" and r1["alerted"] is True
            r2 = await sweep(db)
            assert r2["alerted"] is True
            alerts = await db.ops_alerts.find({"meta.campaign_id": cid}).to_list(10)
            assert len(alerts) == 1 and alerts[0]["severity"] == "critical"
            assert alerts[0]["occurrences"] == 2
        finally:
            await db.soak_campaigns.update_many(
                {"_id": {"$in": [o["_id"] for o in others]}}, {"$set": {"status": "RUNNING"}})
    _run(_t())


def test_soak_report_exposes_watch_block():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "routes", "ops_routes.py")).read()
    assert '"watch": memory_trend(samples)' in src
    cp = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "soak_campaign.py")).read()
    assert 'memory["verdict"] != "ALERT"' in cp
