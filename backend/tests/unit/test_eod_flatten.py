"""iter-53 · EOD flatten unit tests — pure Python, stubbed DB."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


from eod_flatten import (FLATTEN_END_MIN, FLATTEN_START_MIN,  # noqa: E402
                         eod_flatten_block, in_flatten_window,
                         sweep_eod_flatten)


class _AC:
    def __init__(self, docs):
        self.docs = docs

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield d
        return gen()


def _utc(h, m):
    return datetime(2026, 7, 20, h, m, tzinfo=timezone.utc)


class TestWindowMath:
    def test_window_bounds_broker_time(self):
        assert FLATTEN_START_MIN == 23 * 60 + 15
        assert FLATTEN_END_MIN == 23 * 60 + 40   # EA quiet takes over there
        assert in_flatten_window(0, _utc(23, 15)) is True
        assert in_flatten_window(0, _utc(23, 39)) is True
        assert in_flatten_window(0, _utc(23, 40)) is False
        assert in_flatten_window(0, _utc(23, 14)) is False
        assert in_flatten_window(0, _utc(12, 0)) is False

    def test_broker_offset_shifts_window(self):
        # UTC+3 broker (OnEquity): broker 23:20 == 20:20 UTC
        assert in_flatten_window(3 * 3600, _utc(20, 20)) is True
        assert in_flatten_window(3 * 3600, _utc(23, 20)) is False

    def test_block_reason_only_inside_window(self):
        acc = {"broker_utc_offset_sec": 3 * 3600}
        assert eod_flatten_block(acc, _utc(20, 20)) is not None
        assert eod_flatten_block(acc, _utc(10, 0)) is None
        assert eod_flatten_block(None, _utc(23, 20)) is not None  # offset 0

    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("EOD_FLATTEN_ENABLED", "false")
        assert eod_flatten_block({}, _utc(23, 20)) is None


class TestSweep:
    def _db(self, accounts, trades):
        db = MagicMock()
        db.accounts.find = MagicMock(return_value=_AC(accounts))
        db.trades.find = MagicMock(return_value=_AC(trades))
        db.trades.update_one = AsyncMock()
        db.notifications.insert_one = AsyncMock()
        return db

    def test_queues_full_close_for_open_auto_trades(self):
        acc = {"_id": "a1", "broker_utc_offset_sec": 0, "user_id": "u1",
               "name": "Demo"}
        trades = [{"_id": "t1", "origin": "auto", "status": "open"},
                  {"_id": "t2", "origin": "auto", "status": "open",
                   "pending_modification": {"type": "MODIFY_SL"}}]
        db = self._db([acc], trades)
        rc = AsyncMock(return_value={"trades_marked_for_close": 1})   # r25 P2-01 close protocol
        with patch("eod_flatten.request_close", rc):
            out = asyncio.run(sweep_eod_flatten(db, now=_utc(23, 20)))
        assert out == {"enabled": True, "queued": 1, "accounts": 1}
        args = rc.await_args
        assert args.args[1] == {"_id": "t1"}       # pending one untouched
        assert args.kwargs["reason"] == "eod_flatten"
        sets = args.kwargs["stamp"]
        assert sets["pending_modification"]["type"] == "FULL_CLOSE"
        assert sets["pending_modification"]["reason"] == "eod_flatten"
        db.trades.update_one.assert_not_awaited()
        db.notifications.insert_one.assert_awaited_once()

    def test_manual_trades_never_touched(self):
        """The trades query itself is scoped to origin=auto."""
        acc = {"_id": "a1", "broker_utc_offset_sec": 0}
        db = self._db([acc], [])
        asyncio.run(sweep_eod_flatten(db, now=_utc(23, 20)))
        filt = db.trades.find.call_args.args[0]
        assert filt["origin"] == "auto"
        assert filt["status"] == "open"

    def test_account_outside_window_skipped(self):
        acc = {"_id": "a1", "broker_utc_offset_sec": 3 * 3600}  # broker 02:20
        db = self._db([acc], [{"_id": "t1", "origin": "auto",
                               "status": "open"}])
        out = asyncio.run(sweep_eod_flatten(db, now=_utc(23, 20)))
        assert out["queued"] == 0
        db.trades.update_one.assert_not_awaited()

    def test_sweep_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("EOD_FLATTEN_ENABLED", "false")
        db = self._db([{"_id": "a1"}], [])
        out = asyncio.run(sweep_eod_flatten(db, now=_utc(23, 20)))
        assert out == {"enabled": False, "queued": 0, "accounts": 0}
        db.accounts.find.assert_not_called()
