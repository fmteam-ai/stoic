"""Public /api/status must never hang: bounded compute + fail-closed degraded answer."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def test_status_probe_times_out_fail_closed(monkeypatch):
    from routes import portal_routes as pr
    monkeypatch.setattr(pr, "STATUS_COMPUTE_TIMEOUT", 0.05)
    monkeypatch.setattr(pr, "_STATUS_CACHE", {"at": 0.0, "data": None})
    monkeypatch.setattr(pr, "_STATUS_LOCK", None)

    async def _stuck(now, timings=None):
        (timings if timings is not None else {})["db_ping"] = 0.01
        await asyncio.sleep(5)
    monkeypatch.setattr(pr, "_compute_status", _stuck)

    async def run():
        return await asyncio.wait_for(pr.public_status(), 2)
    data = asyncio.run(run())
    assert data["overall"] == "degraded"
    assert data["trading"]["readiness"]["new_exposure_allowed"] is False
    assert data["trading"]["attestation"]["attested"] is False
    assert data["components"]["status_probe"]["status"] == "degraded"
    # degraded answer is cached briefly (no stampede behind the lock) and served instantly
    assert asyncio.run(run())["overall"] == "degraded"


def test_projection_uses_one_grouped_open_position_query():
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    src = open(os.path.join(root, "inventory_projection.py")).read()
    assert 'db.trades.aggregate([{"$match": open_match}' in src
    assert 'await db.trades.count_documents({"account_id": aid, "status": "open"})' not in src
