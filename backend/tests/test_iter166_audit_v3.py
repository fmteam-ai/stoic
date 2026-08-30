"""Audit v3 (2026-08-30) corrections — truth consistency + fail-closed.

Covers:
- P0-2 canonical inventory object (state_contract.inventory)
- P0-5 session-aware stale-feed rule (weekend BTC governed)
- P0-6 capital-stage statistics as HARD autonomous-live promotion gates
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

pytestmark = pytest.mark.integration


def _db():
    from motor.motor_asyncio import AsyncIOMotorClient
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------ P0-5 session-aware feed
class TestSessionAwareStaleFeed:
    def _stale_bars(self, anchor):
        return [{"t": anchor - 3600 - (39 - i) * 900, "o": 100.0,
                 "h": 101.0, "l": 99.0, "c": 100.0} for i in range(40)]

    def test_crypto_stale_feed_blocks_even_on_weekend(self, monkeypatch):
        import risk_engine as re_
        anchor = datetime.now(timezone.utc).timestamp()
        res = re_.abnormal_market_check(self._stale_bars(anchor),
                                        now_ts=anchor, symbol="BTCUSD")
        assert res["status"] == "block", res

    def test_crypto_suffixed_symbol_governed(self):
        import risk_engine as re_
        anchor = datetime.now(timezone.utc).timestamp()
        res = re_.abnormal_market_check(self._stale_bars(anchor),
                                        now_ts=anchor, symbol="BTCUSD.r")
        assert res["status"] == "block", res

    def test_xau_governed_on_weekdays(self):
        import risk_engine as re_
        if datetime.now(timezone.utc).weekday() >= 5:
            pytest.skip("weekend — XAU session assertion not applicable")
        anchor = datetime.now(timezone.utc).timestamp()
        res = re_.abnormal_market_check(self._stale_bars(anchor),
                                        now_ts=anchor, symbol="XAUUSD")
        assert res["status"] == "block", res

    def test_drill_asserts_crypto_every_day(self):
        from chaos_drills import _drill_broker_disconnect
        out = _drill_broker_disconnect()
        assert out["passed"] is True, out
        assert "BTCUSD" in out["detail"]
        assert "skipped_assertions" in out
        if datetime.now(timezone.utc).weekday() >= 5:
            assert out["skipped_assertions"], \
                "weekend must report the XAU assertion as SKIPPED"
            assert "not a pass" in out["skipped_assertions"][0]


# ------------------------------------------------ P0-2 canonical inventory
class TestCanonicalInventory:
    def test_inventory_shape_and_consistency(self):
        async def go():
            db = _db()
            u = await db.users.find_one({"email": "admin@trading.bot"})
            from state_contract import contract, inventory
            uid = str(u["_id"])
            return await inventory(db, uid), await contract(db, uid)
        inv, c = _run(go())
        assert inv["accounts_configured"] == c["totals"]["accounts_total"]
        assert inv["accounts_enabled"] == c["totals"]["accounts_enabled"]
        assert inv["bots_requested_on"] == c["totals"]["bots_enabled"]
        ea = inv["ea_connection"]
        assert (ea["fresh"] + ea["stale"] + ea["offline"] + ea["paper"]
                == inv["accounts_configured"])
        assert isinstance(inv["ea_installation_count"], int)
        assert sum(inv["bots_effective"].values()) \
            == inv["accounts_configured"]
        assert len(inv["accounts"]) == inv["accounts_configured"]
        row = inv["accounts"][0]
        for k in ("account_enabled", "bot_requested_enabled",
                  "bot_effective_state", "ea_connection_state",
                  "environment"):
            assert k in row, row

    def test_owner_intent_accounts_match_bots(self):
        """Audit v3 P0-2 — intended state: accounts ON ⇔ bots ON."""
        async def go():
            db = _db()
            u = await db.users.find_one({"email": "admin@trading.bot"})
            from state_contract import inventory
            return await inventory(db, str(u["_id"]))
        inv = _run(go())
        assert inv["accounts_enabled"] == inv["bots_requested_on"], inv


# ------------------------------------------------ P0-6 hard promotion gate
class TestCapitalStageHardGate:
    def test_autonomous_live_blocked_on_failed_statistics(self):
        async def go():
            db = _db()
            u = await db.users.find_one({"email": "admin@trading.bot"})
            from operational_modes import promotion_gate
            return await promotion_gate(db, str(u["_id"]), None,
                                        "autonomous_live")
        v = _run(go())
        cs = v["evidence"].get("capital_stage")
        assert cs is not None, "capital stage evidence must be attached"
        ev = cs["evidence"]
        gates_fail = (ev["n"] < 300 or ev["ci95_lower_r"] <= 0
                      or (ev["profit_factor"] or 0) < 1.05
                      or ev["max_drawdown_r"] > 40)
        if gates_fail:
            assert v["allowed"] is False
            assert any("capital-stage" in b for b in v["blockers"]), v

    def test_supervised_live_not_blocked_by_capital_stage(self):
        async def go():
            db = _db()
            u = await db.users.find_one({"email": "admin@trading.bot"})
            from operational_modes import promotion_gate
            return await promotion_gate(db, str(u["_id"]), None,
                                        "supervised_live")
        v = _run(go())
        assert not any("capital-stage" in b for b in v.get("blockers", []))
