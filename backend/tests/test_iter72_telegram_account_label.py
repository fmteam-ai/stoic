"""Tests for iter-72 — Telegram notifications include account name."""
from __future__ import annotations
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import asyncio
from unittest.mock import patch, AsyncMock

# Load .env so notifier's get_db() works
try:
    from dotenv import load_dotenv
    load_dotenv(_os.path.join(_BACKEND_DIR, ".env"))
except Exception:
    pass

import pytest
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

import notifier
import database


@pytest.fixture(autouse=True)
def _reset_db_client():
    """Each asyncio.run() creates a new loop; the cached Motor client from
    a previous loop becomes unusable. Reset between tests so notifier's
    get_db() opens a fresh client bound to the current loop."""
    database._client = None
    database._db = None
    yield
    database._client = None
    database._db = None


def _get_db_url():
    mongo_url = "mongodb://localhost:27017"
    db_name = "test_database"
    try:
        with open(_os.path.join(_BACKEND_DIR, ".env")) as f:
            for line in f:
                if line.startswith("MONGO_URL="):
                    mongo_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("DB_NAME="):
                    db_name = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return mongo_url, db_name


async def _with_db(coro_factory):
    mongo_url, db_name = _get_db_url()
    client = AsyncIOMotorClient(mongo_url)
    try:
        return await coro_factory(client[db_name])
    finally:
        client.close()


# ─────────── _title_with_account formatter ───────────
def test_title_no_account_label_passthrough():
    assert notifier._title_with_account("", "Trade Opened") == "Trade Opened"
    assert notifier._title_with_account(None, "x") == "x"


def test_title_label_inserted_after_emoji():
    out = notifier._title_with_account("vtmarkets",
                                       "🟢 Trade Opened · XAUUSD BUY")
    assert "[vtmarkets]" in out
    assert out.startswith("🟢")
    assert out.endswith("Trade Opened · XAUUSD BUY")


def test_title_label_prefixed_when_no_emoji():
    out = notifier._title_with_account("micro", "Break-Even Set")
    assert out == "[micro] Break-Even Set"


# ─────────── _account_label_for resolver ───────────
def test_account_label_for_trade_dict():
    acct_id = ObjectId()

    async def _go(db):
        await db.accounts.insert_one({
            "_id": acct_id, "label": "iter72_vt", "bridge_token": f"iter72_{acct_id}",
            "mode": "live", "status": "connected",
        })
        try:
            return await notifier._account_label_for(
                {"account_id": str(acct_id)},
            )
        finally:
            await db.accounts.delete_one({"_id": acct_id})

    label = asyncio.run(_with_db(_go))
    assert label == "iter72_vt"


def test_account_label_for_explicit_account_id():
    acct_id = ObjectId()

    async def _go(db):
        await db.accounts.insert_one({
            "_id": acct_id, "label": "iter72_micro",
            "bridge_token": f"iter72_m_{acct_id}",
            "mode": "live", "status": "connected",
        })
        try:
            return await notifier._account_label_for(account_id=str(acct_id))
        finally:
            await db.accounts.delete_one({"_id": acct_id})

    assert asyncio.run(_with_db(_go)) == "iter72_micro"


def test_account_label_for_missing_returns_empty_string():
    async def _go(_db):
        return await notifier._account_label_for(account_id=str(ObjectId()))
    assert asyncio.run(_with_db(_go)) == ""


def test_account_label_for_falls_back_to_broker():
    """No label set → returns broker name."""
    acct_id = ObjectId()

    async def _go(db):
        await db.accounts.insert_one({
            "_id": acct_id, "broker": "RoboForex",
            "bridge_token": f"iter72_b_{acct_id}",
            "mode": "live", "status": "connected",
        })
        try:
            return await notifier._account_label_for(account_id=str(acct_id))
        finally:
            await db.accounts.delete_one({"_id": acct_id})

    assert asyncio.run(_with_db(_go)) == "RoboForex"


def test_account_label_for_bad_id_returns_empty():
    async def _go(_db):
        return await notifier._account_label_for(account_id="not-an-objectid")
    assert asyncio.run(_with_db(_go)) == ""


# ─────────── notify_trade_opened sends labelled title ───────────
def test_notify_trade_opened_includes_account_label():
    """A real trade dict from VT Markets should produce a [vtmarkets]-tagged title."""
    acct_id = ObjectId()
    trade_oid = ObjectId()

    async def _seed(db):
        await db.accounts.insert_one({
            "_id": acct_id, "label": "vtmarkets",
            "bridge_token": f"iter72_send_{acct_id}",
            "mode": "live", "status": "connected",
        })
        # Plausibility check requires the trade to exist in DB
        await db.trades.insert_one({
            "_id": trade_oid, "account_id": str(acct_id),
            "user_id": "u_x", "symbol": "XAUUSD", "action": "BUY",
            "lot_size": 0.1, "entry_price": 4050.0,
            "stop_loss": 4040.0, "take_profit": 4070.0,
            "mt5_ticket": 477720804, "status": "open",
        })

    async def _cleanup(db):
        await db.accounts.delete_one({"_id": acct_id})
        await db.trades.delete_one({"_id": trade_oid})

    asyncio.run(_with_db(_seed))
    try:
        sent_titles: list = []
        async def fake_send(_uid, _evt, title, _lines):
            sent_titles.append(title)
            return True

        async def _go(_db):
            # Patch market quote to avoid live HTTP
            with patch("notifier.send_telegram", side_effect=fake_send):
                await notifier.notify_trade_opened("u_x", {
                    "_id": trade_oid,
                    "id": str(trade_oid),
                    "account_id": str(acct_id),
                    "symbol": "XAUUSD", "action": "BUY",
                    "lot_size": 0.1, "entry_price": 4050.0,
                    "stop_loss": 4040.0, "take_profit": 4070.0,
                    "mt5_ticket": 477720804,
                    "origin": "auto",
                })

        # Patch quote inside the helper so plausibility passes without HTTP.
        with patch("market.get_quote", new=AsyncMock(return_value={"price": 4055.0})):
            asyncio.run(_with_db(_go))

        assert sent_titles, "no telegram message was sent"
        title = sent_titles[0]
        assert "[vtmarkets]" in title, f"account label missing from: {title!r}"
        assert "Trade Opened" in title
        assert "XAUUSD" in title
    finally:
        asyncio.run(_with_db(_cleanup))


def test_notify_breakeven_includes_label():
    """notify_breakeven looks up the trade by id to pick up the label."""
    acct_id = ObjectId()
    trade_oid = ObjectId()

    async def _seed(db):
        await db.accounts.insert_one({
            "_id": acct_id, "label": "micro",
            "bridge_token": f"iter72_be_{acct_id}",
            "mode": "live", "status": "connected",
        })
        await db.trades.insert_one({
            "_id": trade_oid, "account_id": str(acct_id),
            "user_id": "u_y", "symbol": "XAUUSD",
        })

    async def _cleanup(db):
        await db.accounts.delete_one({"_id": acct_id})
        await db.trades.delete_one({"_id": trade_oid})

    asyncio.run(_with_db(_seed))
    try:
        sent: list = []
        async def fake_send(_uid, _evt, title, _lines):
            sent.append(title)
            return True

        async def _go(_db):
            with patch("notifier.send_telegram", side_effect=fake_send):
                await notifier.notify_breakeven("u_y", str(trade_oid), 4050.0, 1.0)
        asyncio.run(_with_db(_go))
        assert sent and "[micro]" in sent[0]
        assert "Break-Even Set" in sent[0]
    finally:
        asyncio.run(_with_db(_cleanup))


def test_notify_account_blocked_includes_label():
    acct_id = ObjectId()

    async def _seed(db):
        await db.accounts.insert_one({
            "_id": acct_id, "label": "vtmarkets",
            "bridge_token": f"iter72_ab_{acct_id}",
            "mode": "live", "status": "connected",
        })

    async def _cleanup(db):
        await db.accounts.delete_one({"_id": acct_id})

    asyncio.run(_with_db(_seed))
    try:
        sent: list = []
        async def fake_send(_uid, _evt, title, lines):
            sent.append((title, lines))
            return True

        async def _go(_db):
            with patch("notifier.send_telegram", side_effect=fake_send):
                await notifier.notify_account_blocked(
                    "u_z", str(acct_id),
                    "10013", "INVALID_REQUEST",
                    "Symbol name mismatch (broker uses suffix)",
                )
        asyncio.run(_with_db(_go))
        assert sent
        title, lines = sent[0]
        assert "[vtmarkets]" in title
        assert "Auto-Halted" in title
        assert any("10013" in str(line) for line in lines)
    finally:
        asyncio.run(_with_db(_cleanup))


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
