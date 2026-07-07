"""iter-58 · Loss Cooldown guardrail — global (cross-account) symbol+direction
pause after any loss."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loss_cooldown import loss_cooldown_block  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *a):
        return self

    def limit(self, *a):
        return self

    async def to_list(self, length=None):
        return self.rows


class FakeTrades:
    def __init__(self, rows):
        self.rows = rows
        self.last_query = None

    def find(self, q):
        self.last_query = q
        return FakeCursor(self.rows)


class FakeDB:
    def __init__(self, rows):
        self.trades = FakeTrades(rows)


def loss(sym="XAUUSD", action="SELL", pnl=-50.0, minutes_ago=10):
    return {"symbol": sym, "action": action, "pnl": pnl,
            "closed_at": (datetime.now(timezone.utc)
                          - timedelta(minutes=minutes_ago)).isoformat()}


@pytest.mark.asyncio
async def test_blocks_same_direction_after_recent_loss():
    db = FakeDB([loss(minutes_ago=10)])
    r = await loss_cooldown_block(db, "u1", "XAUUSD", "SELL", {})
    assert r is not None and "Loss cooldown" in r


@pytest.mark.asyncio
async def test_suffix_symbols_match_base():
    db = FakeDB([loss(sym="GOLD#", minutes_ago=5)])
    r = await loss_cooldown_block(db, "u1", "XAUUSD-ECN", "SELL", {})
    assert r is not None


@pytest.mark.asyncio
async def test_other_symbol_not_blocked():
    db = FakeDB([loss(sym="US30", minutes_ago=5)])
    assert await loss_cooldown_block(db, "u1", "XAUUSD", "SELL", {}) is None


@pytest.mark.asyncio
async def test_query_scopes_direction_and_window():
    db = FakeDB([])
    await loss_cooldown_block(db, "u1", "XAUUSD", "BUY", {})
    q = db.trades.last_query
    assert q["action"] == "BUY" and q["pnl"] == {"$lt": 0}
    assert q["status"] == "closed" and "closed_at" in q


@pytest.mark.asyncio
async def test_disabled_via_cfg():
    db = FakeDB([loss(minutes_ago=1)])
    assert await loss_cooldown_block(db, "u1", "XAUUSD", "SELL",
                                     {"loss_cooldown_enabled": False}) is None


@pytest.mark.asyncio
async def test_custom_minutes_in_query_window():
    db = FakeDB([])
    await loss_cooldown_block(db, "u1", "XAUUSD", "SELL",
                              {"loss_cooldown_minutes": 60})
    cutoff = datetime.fromisoformat(db.trades.last_query["closed_at"]["$gte"])
    delta = (datetime.now(timezone.utc) - cutoff).total_seconds() / 60
    assert 59 <= delta <= 61


@pytest.mark.asyncio
async def test_zero_minutes_disables():
    db = FakeDB([loss(minutes_ago=1)])
    assert await loss_cooldown_block(db, "u1", "XAUUSD", "SELL",
                                     {"loss_cooldown_minutes": 0}) is None


def test_bot_runner_wiring():
    src = open(os.path.join(BACKEND, "bot_runner.py")).read()
    assert "loss_cooldown_block" in src and '"loss_cooldown_block"' in src
