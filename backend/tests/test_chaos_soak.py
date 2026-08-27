"""Chaos + soak suites — never run in ordinary CI (marker-gated)."""
import asyncio
import os

import pytest


@pytest.mark.chaos
def test_chaos_drills_all_pass():
    """Full fault-injection drill battery against the live preview DB."""
    from motor.motor_asyncio import AsyncIOMotorClient

    async def _run():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        from chaos_drills import run_drills
        return await run_drills(db)

    out = asyncio.run(_run())
    failed = [d for d in out.get("drills", []) if not d.get("passed")]
    assert not failed, f"chaos drills failed: {failed}"


@pytest.mark.soak
def test_soak_campaign_status_reachable():
    """Soak status endpoint answers (run against a live environment)."""
    import requests
    base = os.environ.get("REACT_APP_BACKEND_URL")
    if not base:
        pytest.skip("no live backend configured")
    r = requests.get(f"{base}/api/ops/soak/status", timeout=15)
    # unauthenticated must be rejected, not error
    assert r.status_code in (401, 403)
