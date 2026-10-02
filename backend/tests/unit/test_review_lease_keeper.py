"""Review fix: a worker whose lease renewals keep FAILING must stop its loops
before the lease can expire server-side (split-brain guard)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import workers.base as base

pytestmark = pytest.mark.unit


def test_lease_keeper_gives_up_when_renewals_keep_failing():
    async def run():
        lost = asyncio.Event()
        with patch.object(base, "LEASE_RENEW_SEC", 0.01), \
             patch.object(base, "LEASE_TTL_SEC", 0.05), \
             patch.object(base, "get_db", return_value=MagicMock()), \
             patch.object(base, "_try_acquire",
                          AsyncMock(side_effect=RuntimeError("mongo down"))):
            await asyncio.wait_for(base._lease_keeper("trading", lost), 2.0)
        return lost.is_set()

    assert asyncio.run(run()) is True


def test_lease_keeper_tolerates_a_transient_renew_error():
    calls = {"n": 0}

    async def flaky(db, name):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("blip")
        if calls["n"] >= 6:
            return False          # end the test: lease genuinely lost
        return True

    async def run():
        lost = asyncio.Event()
        db = MagicMock()
        db.worker_leases.update_one = AsyncMock()
        with patch.object(base, "LEASE_RENEW_SEC", 0.01), \
             patch.object(base, "LEASE_TTL_SEC", 0.2), \
             patch.object(base, "get_db", return_value=db), \
             patch.object(base, "_try_acquire", flaky):
            await asyncio.wait_for(base._lease_keeper("trading", lost), 2.0)
        return calls["n"]

    # one blip did not stop the worker; it kept renewing until told it lost
    assert asyncio.run(run()) == 6
