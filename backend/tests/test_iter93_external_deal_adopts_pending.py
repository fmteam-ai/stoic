"""iter-93 · /bridge/external-deal must ADOPT the pending sibling STOIC
trade instead of creating a phantom empty-SL/TP duplicate.

Race scenario (observed in prod on 2026-07-01):
  1. execution.py inserts pending trade doc with status='pending',
     mt5_ticket=None, populated stop_loss/take_profit
  2. EA opens the position at the broker and issues two async callbacks:
       a) /bridge/report        → attaches mt5_ticket + flips to 'open'
       b) /bridge/external-deal → OnTradeTransaction 'in' event
  3. If (b) arrives before (a), the ticket-lookup misses. Pre-fix code
     dropped through the fresh-insert path which hardcodes
     stop_loss=0.0 and take_profit=0.0 (BridgeExternalDeal has no SL/TP
     fields to copy). Users got open positions with zero stops.

Post-fix behaviour: when (b) arrives first AND magic=STOIC_MAGIC (901234),
the handler finds the pending sibling by (account, symbol, action) within
a 12-min window and adopts it — attaching the ticket, flipping status
and preserving the original SL/TP.
"""
from __future__ import annotations
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

_BACKEND_DIR = _BACKEND_DIR
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
with open(f"{_BACKEND_DIR}/.env") as _f:
    for _ln in _f:
        if "=" in _ln and not _ln.lstrip().startswith("#"):
            _k, _v = _ln.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

from bson import ObjectId  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_external_deal_adopts_pending_sibling_and_preserves_sltp():
    async def _body():
        from routes import bridge_routes as br
        from database import get_db

        # Route through an isolated test DB so we don't touch real trades.
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        test_db_name = os.environ["DB_NAME"] + "_test_iter93"
        db = client[test_db_name]
        try:
            # Monkey-patch bridge_routes' db accessor for the duration of test
            br.get_db = lambda: db  # type: ignore[assignment]

            account_id = str(uuid.uuid4()).replace("-", "")[:24]
            user_id = str(uuid.uuid4()).replace("-", "")[:24]
            token = f"tok-{uuid.uuid4().hex[:8]}"

            await db.accounts.insert_one({
                "_id": ObjectId(account_id),
                "user_id": user_id,
                "bridge_token": token,
                "broker": "TestBroker",
            })
            now_iso = datetime.now(timezone.utc).isoformat()
            pending = await db.trades.insert_one({
                "user_id": user_id,
                "account_id": account_id,
                "symbol": "XAUUSD-ECN",
                "action": "SELL",
                "lot_size": 0.12,
                "entry_price": 3989.55,
                "stop_loss": 3999.55,
                "take_profit": 3979.55,
                "status": "pending",
                "mt5_ticket": None,
                "opened_at": now_iso,
            })

            payload = br.BridgeExternalDeal(
                bridge_token=token,
                mt5_ticket=485409900,
                deal_id=int(datetime.now().timestamp() * 1000),
                deal_entry="in",
                symbol="XAUUSD-ECN",
                action="SELL",
                lots=0.12,
                price=3989.55,
                deal_time=int(datetime.now().timestamp()),
                magic=901234,
            )
            resp = await br.external_deal(payload)
            assert resp.get("ok") is True, resp
            assert "adopted" in resp, f"Expected adoption, got {resp}"

            adopted = await db.trades.find_one({"_id": pending.inserted_id})
            assert adopted["status"] == "open"
            assert adopted["mt5_ticket"] == 485409900
            assert adopted["stop_loss"] == 3999.55, "SL was dropped — regression!"
            assert adopted["take_profit"] == 3979.55, "TP was dropped — regression!"
            assert adopted["adopted_via_external_deal"] is True

            total = await db.trades.count_documents({"mt5_ticket": 485409900})
            assert total == 1, f"Expected 1 trade for ticket, found {total}"
        finally:
            await client.drop_database(test_db_name)
            client.close()

    _run(_body())


def test_external_deal_manual_trades_still_create_fresh():
    async def _body():
        from routes import bridge_routes as br

        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        test_db_name = os.environ["DB_NAME"] + "_test_iter93b"
        db = client[test_db_name]
        try:
            br.get_db = lambda: db  # type: ignore[assignment]

            account_id = str(uuid.uuid4()).replace("-", "")[:24]
            user_id = str(uuid.uuid4()).replace("-", "")[:24]
            token = f"tok-{uuid.uuid4().hex[:8]}"

            await db.accounts.insert_one({
                "_id": ObjectId(account_id),
                "user_id": user_id,
                "bridge_token": token,
                "broker": "TestBroker",
            })

            payload = br.BridgeExternalDeal(
                bridge_token=token,
                mt5_ticket=999888777,
                deal_id=int(datetime.now().timestamp() * 1000),
                deal_entry="in",
                symbol="EURUSD",
                action="BUY",
                lots=0.5,
                price=1.0850,
                deal_time=int(datetime.now().timestamp()),
                magic=0,  # manual — no STOIC pending doc to adopt
            )
            resp = await br.external_deal(payload)
            assert resp.get("ok") is True
            assert "created" in resp
            fresh = await db.trades.find_one({"mt5_ticket": 999888777})
            assert fresh["origin"] == "manual"
            assert fresh["external_open"] is True
        finally:
            await client.drop_database(test_db_name)
            client.close()

    _run(_body())
