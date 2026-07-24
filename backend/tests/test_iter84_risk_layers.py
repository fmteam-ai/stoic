"""iter-84 · Multi-layer risk engine — portfolio stop + layer isolation."""
import asyncio
import os
import pathlib
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).resolve().parents[1] / ".env")

from database import get_db  # noqa: E402
from risk_layers import (LAYERS, _isolated, check_portfolio_stop,  # noqa: E402
                         evaluate_layers, floating_drawdown_pct,
                         sweep_portfolio_stop)


def test_twelve_layers_registered():
    keys = [k for k, _, _ in LAYERS]
    assert len(keys) == 12 and len(set(keys)) == 12
    for expected in ("strategy_stop", "position_stop", "portfolio_stop",
                     "daily_loss_stop", "weekly_loss_stop",
                     "monthly_drawdown_stop", "volatility_stop",
                     "spread_protection", "liquidity_protection",
                     "broker_anomaly_protection", "news_protection",
                     "circuit_breaker"):
        assert expected in keys


def test_floating_drawdown_math():
    assert floating_drawdown_pct({"balance": 10000, "equity": 8500}) == -15.0
    assert floating_drawdown_pct({"balance": 10000, "equity": 10200}) == 2.0
    assert floating_drawdown_pct({"balance": 0, "equity": 1}) is None
    assert floating_drawdown_pct({"balance": 10000}) is None


def test_isolated_layer_error_cannot_break_others():
    async def boom():
        raise RuntimeError("layer exploded")
    res = asyncio.get_event_loop().run_until_complete(_isolated(boom))
    assert res["status"] == "error"
    assert "layer exploded" in res["detail"]


@pytest.fixture()
def loop():
    return asyncio.get_event_loop()


class TestPortfolioStopLive:
    """Round-trips against the real DB with a synthetic account."""

    def test_trip_disable_and_no_retrip(self, loop):
        async def run():
            db = get_db()
            now = datetime.now(timezone.utc).isoformat()
            acc = await db.accounts.insert_one({
                "user_id": "iter84-test-user", "label": "TEST_iter84_pstop",
                "bridge_token": "iter84-pstop-tok",
                "balance": 10000.0, "equity": 8000.0,
                "last_heartbeat": now, "status": "active"})
            cfg = await db.bot_configs.insert_one({
                "user_id": "iter84-test-user",
                "account_id": str(acc.inserted_id), "active": True})
            try:
                account = await db.accounts.find_one({"_id": acc.inserted_id})
                res = await check_portfolio_stop(db, account)
                assert res["status"] == "tripped", res
                c = await db.bot_configs.find_one({"_id": cfg.inserted_id})
                assert c["active"] is False
                assert c["tripped_kind"] == "portfolio"
                # second evaluation must not re-trip (config now inactive)
                res2 = await check_portfolio_stop(db, account)
                assert res2["status"] == "already_tripped"
                alert = await db.ops_alerts.find_one(
                    {"dedup_key": f"portfolio_stop:{acc.inserted_id}"})
                assert alert and alert["severity"] == "critical"
            finally:
                await db.accounts.delete_one({"_id": acc.inserted_id})
                await db.bot_configs.delete_one({"_id": cfg.inserted_id})
                await db.ops_alerts.delete_many(
                    {"dedup_key": f"portfolio_stop:{acc.inserted_id}"})
                await db.trade_events.delete_many(
                    {"event_type": "PortfolioStopTripped",
                     "account_id": str(acc.inserted_id)})
        loop.run_until_complete(run())

    def test_healthy_account_never_trips(self, loop):
        async def run():
            db = get_db()
            now = datetime.now(timezone.utc).isoformat()
            acc = await db.accounts.insert_one({
                "user_id": "iter84-test-user", "label": "TEST_iter84_healthy",
                "bridge_token": "iter84-healthy-tok",
                "balance": 10000.0, "equity": 9900.0,
                "last_heartbeat": now, "status": "active"})
            cfg = await db.bot_configs.insert_one({
                "user_id": "iter84-test-user",
                "account_id": str(acc.inserted_id), "active": True})
            try:
                account = await db.accounts.find_one({"_id": acc.inserted_id})
                res = await check_portfolio_stop(db, account)
                assert res["status"] == "armed", res
                c = await db.bot_configs.find_one({"_id": cfg.inserted_id})
                assert c["active"] is True
            finally:
                await db.accounts.delete_one({"_id": acc.inserted_id})
                await db.bot_configs.delete_one({"_id": cfg.inserted_id})
        loop.run_until_complete(run())

    def test_stale_heartbeat_is_failsafe(self, loop):
        async def run():
            db = get_db()
            acc = await db.accounts.insert_one({
                "user_id": "iter84-test-user", "label": "TEST_iter84_stale",
                "bridge_token": "iter84-stale-tok",
                "balance": 10000.0, "equity": 5000.0,
                "last_heartbeat": "2020-01-01T00:00:00+00:00",
                "status": "active"})
            try:
                account = await db.accounts.find_one({"_id": acc.inserted_id})
                res = await check_portfolio_stop(db, account)
                assert res["status"] == "degraded", res
            finally:
                await db.accounts.delete_one({"_id": acc.inserted_id})
        loop.run_until_complete(run())


def test_evaluate_layers_returns_all_twelve(loop=None):
    async def run():
        db = get_db()
        layers = await evaluate_layers(db, "iter84-nonexistent-user", None)
        assert len(layers) == 12
        for l in layers:
            assert l["status"] in ("armed", "tripped", "degraded", "error")
            assert l["detail"]
    asyncio.get_event_loop().run_until_complete(run())
