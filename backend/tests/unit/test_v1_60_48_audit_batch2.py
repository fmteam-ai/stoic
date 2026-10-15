"""v1.60.48 — feature audit 15 Oct, batch 2: Strategy Generator (a–d), intelligence counters whitelist, engine.execute
regime/strategy_class forwarding, Macro card real yield, agent_activity account_id, quote timeout/retry + Strategy ceiling."""
import asyncio
import os
import sys
from unittest.mock import patch, AsyncMock

import pytest
from bson import ObjectId
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND = os.path.join(os.path.dirname(BACKEND), "frontend", "src")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ----------------------------------------------------------------------------------------------- Strategy Generator
def _apply(db, compiled, target, **extra):
    import routes.nl_routes as nr
    with patch.object(nr, "get_db", lambda: db):
        return _run(nr.nl_strategy_apply({"compiled": compiled, "target": target, **extra}, user={"id": "u1"}))


def test_daily_limit_maps_to_trade_of_day_cap_and_auto_execute_is_kept_unless_asked():
    db = FakeDb()
    cid = ObjectId()
    db.bot_configs.rows.append({"_id": cid, "user_id": "u1", "account_id": "A", "symbols": ["XAUUSD"],
                                "auto_execute": True, "max_concurrent_trades": 3, "trade_of_day_cap": 1})
    out = _apply(db, {"risk_level": "low", "symbols": ["XAUUSD"], "max_trades_per_day": 2,
                      "strategy_style": "scalping", "session_preference": "london"}, "A")
    cfg = db.bot_configs.rows[0]
    assert cfg["trade_of_day_cap"] == 2 and cfg["max_concurrent_trades"] == 3, "daily limit must not touch concurrency"
    assert cfg["auto_execute"] is True, "(c) auto_execute untouched when the prompt did not mention it"
    assert cfg["active_preset"] == "scalper" and cfg["session_preference"] == "london"
    assert out["undo"] and out["undo"][0]["version_id"] and out["diff"][0]["changes"]["trade_of_day_cap"] == {"from": 1, "to": 2}
    _apply(db, {"risk_level": "low", "symbols": ["XAUUSD"], "auto_execute": False}, "A")
    assert db.bot_configs.rows[0]["auto_execute"] is False


def test_deploy_defaults_to_one_bot_and_preview_does_not_write():
    db = FakeDb()
    db.bot_configs.rows += [{"_id": ObjectId(), "user_id": "u1", "account_id": a, "symbols": ["XAUUSD"], "risk_level": "high"}
                            for a in ("A", "B")]
    with pytest.raises(HTTPException) as ei:
        _apply(db, {"risk_level": "low", "symbols": ["XAUUSD"]}, "matching")
    assert ei.value.status_code == 409 and ei.value.detail["code"] == "multi_target_confirm_required"
    out = _apply(db, {"risk_level": "low", "symbols": ["XAUUSD"]}, "all", confirm_multi=True, preview=True)
    assert out["preview"] is True and len(out["diff"]) == 2
    assert all(c["risk_level"] == "high" for c in db.bot_configs.rows), "preview must not write"
    assert not db.config_versions.rows
    prompt = open(os.path.join(BACKEND, "nl_commander.py")).read()
    assert "max_trades_per_day" in prompt and "Omit auto_execute unless" in prompt
    page = open(os.path.join(FRONTEND, "pages", "Strategies.jsx")).read()
    assert 'useState("matching")' not in page and "PREVIEW DEPLOY" in page and 'data-testid="strategy-deploy-preview"' in page
    assert 'target: "default"' in open(os.path.join(FRONTEND, "pages", "RiskCommander.jsx")).read()


def test_session_preference_is_enforced_by_bot_runner():
    src = open(os.path.join(BACKEND, "bot_runner.py")).read()
    assert 'cfg.get("session_preference")' in src and "session_preference_block" in src
    from microstructure import current_session
    from datetime import datetime, timezone
    assert "london" not in current_session(datetime(2026, 10, 14, 3, 0, tzinfo=timezone.utc))["active_sessions"]
    assert "london" in current_session(datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc))["active_sessions"]


# ----------------------------------------------------------------------------------------------- counters
def test_intelligence_counters_store_every_gate_slug():
    import intelligence_counters as ic
    db = FakeDb()
    with patch.object(ic, "get_db", lambda: db):
        for k in ("mc_block", "liquidity_gate_veto", "exhaustion_chase_veto", "eod_quiet_block", "loss_cooldown_block",
                  "structure_gate_veto", "mtf_veto", "user_direction_cap_block"):
            _run(ic.increment("u1", k))
        _run(ic.increment("u1", "$bad.key"))
        _run(ic.increment("u1", "user_id"))
    row = db.intelligence_counters.rows[0]
    assert row["mc_block"] == 1 and row["structure_gate_veto"] == 1 and row["mtf_veto"] == 1
    assert "$bad.key" not in row and row["user_id"] == "u1"


# ----------------------------------------------------------------------------------------------- engine.execute
def test_engine_execute_receives_market_regime_and_strategy_class():
    src = open(os.path.join(BACKEND, "bot_runner.py")).read()
    seg = src[src.index("trade_doc = await engine.execute("):]
    seg = seg[:seg.index("max_concurrent=max_concurrent")]
    assert '"strategy_class": signal.get("strategy_class")' in seg and '"market_regime": signal.get("market_regime")' in seg


# ----------------------------------------------------------------------------------------------- macro card
def test_macro_endpoint_exposes_real_yield_and_card_shows_it():
    import routes.agent_routes as ar

    async def snap():
        return {"series": {"DGS10": {"value": 4.1}}}

    async def ry():
        return {"real_yield_10y": 1.84, "real_yield_10y_date": "2026-10-14", "regime": "high_real_yield", "delta_5d": -0.05, "source": "fred"}
    with patch.object(ar, "get_macro_snapshot", snap), patch("macro.tips.get_real_yield", ry):
        out = _run(ar.macro_snapshot(user={"id": "u1"}))
    assert out["series"]["REAL10Y"] == {"value": 1.84, "date": "2026-10-14", "regime": "high_real_yield",
                                        "delta_30d": -0.05, "delta_label": "5d", "source": "fred"}
    page = open(os.path.join(FRONTEND, "pages", "Agents.jsx")).read()
    assert '"REAL10Y"' in page and '"DFF", "DGS10"' not in page


# ----------------------------------------------------------------------------------------------- activity / latency
def test_agent_activity_tagged_with_account_and_strategy_has_a_timeout():
    from agents import orchestrator as o
    assert o._acct_id({"_id": ObjectId("a" * 24)}, {"account_id": "x"}) == "a" * 24
    assert o._acct_id(None, {"account_id": "x"}) == "x" and o._acct_id(None, None) is None
    src = open(os.path.join(BACKEND, "agents", "orchestrator.py")).read()
    assert src.count('"account_id": _acct_id(account, user_cfg)') == 2
    assert "asyncio.wait_for(\n                self.strategy.propose(" in src and o.STRATEGY_TIMEOUT_SEC > 0


def test_quote_fetch_retries_then_serves_last_good_quote():
    import market
    market._LAST_GOOD.clear()
    market._cache_set("quote:XAUUSD", None, 0) if hasattr(market, "_cache_set") else None
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise market.httpx.ConnectError("boom")
        return {"price": 4000.0, "bid": 4000.0, "ask": 4000.2, "change": 0, "change_pct": 0, "high": None, "low": None, "timestamp": "t"}
    with patch.object(market, "_gold_quote", flaky), patch.object(market, "QUOTE_RETRY_DELAY_SEC", 0), \
            patch.object(market, "_cache_get", lambda k: None), patch("database.get_db", side_effect=RuntimeError("no db")):
        q = _run(market.get_quote("XAUUSD"))
    assert q["price"] == 4000.0 and calls["n"] == 2 and not q.get("stale")

    async def dead():
        raise market.httpx.ConnectError("down")
    with patch.object(market, "_gold_quote", dead), patch.object(market, "QUOTE_RETRY_DELAY_SEC", 0), \
            patch.object(market, "_cache_get", lambda k: None):
        q2 = _run(market.get_quote("XAUUSD"))
    assert q2["stale"] is True and q2["price"] == 4000.0
    market._LAST_GOOD.clear()
    with patch.object(market, "_gold_quote", dead), patch.object(market, "QUOTE_RETRY_DELAY_SEC", 0), \
            patch.object(market, "_cache_get", lambda k: None), pytest.raises(RuntimeError):
        _run(market.get_quote("XAUUSD"))
