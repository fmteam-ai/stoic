"""iter-46 true-staleness rejection unit test.

Verifies that persist_risk_now / _fenced_account_write STILL reject when the
existing scalp_risk_state doc holds a STRICTLY GREATER lease_epoch than the
runner's epoch. Complements the iter-45 fix which only relaxes the equal /
older case (self-race against the worker's own bg _persist_risk).
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scalp.engine import ScalpRunner, _lease_epoch  # noqa: E402


def _stub_db(existing_epoch: int):
    """Stub whose scalp_risk_state.update_one returns matched_count=0 (fence
    missed) and find_one returns a doc with the given lease_epoch."""
    db = MagicMock()
    for coll in ("scalp_risk_state",):
        c = getattr(db, coll)
        c.insert_one = AsyncMock()
        c.update_one = AsyncMock(return_value=MagicMock(matched_count=0,
                                                        modified_count=0))
        c.find_one = AsyncMock(return_value={"lease_epoch": existing_epoch})
    return db


class TestTrueStalenessStillRejected:
    def test_persist_risk_now_rejects_strictly_newer_epoch(self):
        r = ScalpRunner("accSTL1", "uSTL", "EURUSD")
        # Runner lease epoch = 3
        _lease_epoch["accSTL1"] = 3
        # Existing doc holds epoch 5 → strictly newer, must raise
        db = _stub_db(existing_epoch=5)
        with pytest.raises(RuntimeError, match="risk persist fenced out"):
            asyncio.run(r.persist_risk_now(db))
        # cleanup
        _lease_epoch.pop("accSTL1", None)

    def test_persist_risk_now_accepts_equal_epoch_and_retries(self):
        """Equal epoch is NOT stale — retry succeeds (matched_count=1 on retry)."""
        r = ScalpRunner("accSTL2", "uSTL", "EURUSD")
        _lease_epoch["accSTL2"] = 4
        db = MagicMock()
        c = db.scalp_risk_state
        c.insert_one = AsyncMock()
        # First call misses fence; second call (retry) hits.
        c.update_one = AsyncMock(side_effect=[
            MagicMock(matched_count=0, modified_count=0),  # initial miss
            MagicMock(matched_count=1, modified_count=1),  # retry succeeds
            MagicMock(matched_count=1, modified_count=1),  # _ACCOUNT initial
        ])
        c.find_one = AsyncMock(return_value={"lease_epoch": 4})
        # Should NOT raise
        asyncio.run(r.persist_risk_now(db))
        _lease_epoch.pop("accSTL2", None)

    def test_persist_risk_now_accepts_older_epoch_and_retries(self):
        """Existing doc with older epoch also should NOT be treated as stale."""
        r = ScalpRunner("accSTL3", "uSTL", "EURUSD")
        _lease_epoch["accSTL3"] = 7
        db = MagicMock()
        c = db.scalp_risk_state
        c.insert_one = AsyncMock()
        c.update_one = AsyncMock(side_effect=[
            MagicMock(matched_count=0, modified_count=0),
            MagicMock(matched_count=1, modified_count=1),
            MagicMock(matched_count=1, modified_count=1),
        ])
        c.find_one = AsyncMock(return_value={"lease_epoch": 2})
        asyncio.run(r.persist_risk_now(db))
        _lease_epoch.pop("accSTL3", None)

    def test_fenced_account_write_rejects_strictly_newer_epoch(self):
        r = ScalpRunner("accSTL4", "uSTL", "EURUSD")
        _lease_epoch["accSTL4"] = 3
        # Make the per-symbol write succeed on first try (matched_count=1)
        # then _ACCOUNT write miss + find_one returns strictly newer epoch.
        db = MagicMock()
        c = db.scalp_risk_state
        c.insert_one = AsyncMock()
        c.update_one = AsyncMock(side_effect=[
            MagicMock(matched_count=1, modified_count=1),  # per-symbol OK
            MagicMock(matched_count=0, modified_count=0),  # _ACCOUNT miss
        ])
        c.find_one = AsyncMock(return_value={"lease_epoch": 10})
        with pytest.raises(RuntimeError, match="account risk persist fenced out"):
            asyncio.run(r.persist_risk_now(db))
        _lease_epoch.pop("accSTL4", None)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
