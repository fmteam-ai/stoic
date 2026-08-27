"""iter-201 — Phase 2 hardening:
1) Canary promotion ladder: shadow-qualified challengers ramp
   5% → 10% → 25% → 50% → 100% with automatic distribution-deviation
   rollback — never a direct jump to full production.
2) Distributed token-bucket rate limiter (Redis / Mongo fallback) keyed
   by API key + tenant + endpoint class."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


UID = f"u-canary-{uuid.uuid4().hex[:6]}"


def _cleanup():
    db = _db()
    _run(db.shadow_models.delete_many({"user_id": UID}))
    _run(db.bot_configs.delete_many({"user_id": UID}))
    _run(db.signals.delete_many({"user_id": UID}))
    _run(db.trades.delete_many({"user_id": UID}))
    _run(db.rate_buckets.delete_many(
        {"_id": {"$regex": "^rltest-"}}))


def _seed_model(ready=True, status="testing"):
    """A shadow model whose challenger state passes every P3 gate."""
    db = _db()
    from bayes_opt import new_replay_state
    ch = new_replay_state()
    ch.update({"trades": 60, "wins": 40, "losses": 20, "total_r": 30.0,
               "gross_win_r": 55.0, "gross_loss_r": 25.0, "max_dd": 5.0})
    bl = new_replay_state()
    bl.update({"trades": 60, "wins": 25, "losses": 35, "total_r": 5.0,
               "gross_win_r": 30.0, "gross_loss_r": 25.0})
    registered = (datetime.now(timezone.utc)
                  - timedelta(days=20 if ready else 1)).isoformat()
    doc = {"user_id": UID, "engine": "trend", "symbol": "XAUUSD",
           "version": f"trend~{uuid.uuid4().hex[:10]}",
           "params": {"x": 1.0}, "baseline_params": {"x": 0.5},
           "status": status, "registered_at": registered,
           "challenger_state": ch, "baseline_state": bl}
    rid = _run(db.shadow_models.insert_one(doc)).inserted_id
    _run(db.bot_configs.insert_one(
        {"user_id": UID, "active": True, "engine_params": {}}))
    return str(rid)


class TestCanaryLadder:
    def teardown_method(self):
        _cleanup()

    def test_promotion_starts_at_5pct_not_100(self):
        from canary_promotion import start_canary
        db = _db()
        mid = _seed_model()
        out = _run(start_canary(db, UID, mid))
        assert out["allocation_pct"] == 5
        assert out["ladder"] == [5, 10, 25, 50, 100]
        cfg = _run(db.bot_configs.find_one({"user_id": UID}))
        canary = cfg["engine_params_canary"]["trend"]
        assert canary["allocation_pct"] == 5
        # production params UNTOUCHED — no jump to 100%
        assert "trend" not in (cfg.get("engine_params") or {})
        m = _run(db.shadow_models.find_one({"user_id": UID}))
        assert m["status"] == "canary"
        assert m["canary"]["expected"]["win_rate"] == round(40 / 60, 3)

    def test_unqualified_model_cannot_start_canary(self):
        from canary_promotion import start_canary
        db = _db()
        mid = _seed_model(ready=False)
        with pytest.raises(ValueError, match="promotion gate"):
            _run(start_canary(db, UID, mid))

    def _seed_canary_trades(self, model_id, wins, losses, r_win=1.5,
                            r_loss=-1.0):
        db = _db()
        now = datetime.now(timezone.utc).isoformat()
        for i in range(wins + losses):
            is_win = i < wins
            sid = _run(db.signals.insert_one(
                {"user_id": UID, "created_at": now,
                 "canary": {"model_id": str(model_id),
                            "engine": "trend"}})).inserted_id
            r = r_win if is_win else r_loss
            _run(db.trades.insert_one(
                {"user_id": UID, "signal_id": str(sid), "status": "closed",
                 "action": "BUY", "pnl": 10.0 if is_win else -10.0,
                 "entry_price": 100.0, "stop_loss": 99.0,
                 "exit_price": 100.0 + r}))

    def test_deviating_canary_rolls_back_automatically(self):
        """Realized distribution far below expectation → auto rollback and
        the challenger params are removed from bot configs."""
        from canary_promotion import evaluate_canaries, start_canary
        db = _db()
        mid = _seed_model()
        _run(start_canary(db, UID, mid))
        # expected win_rate 0.667 — feed 2/12 wins (0.167, way below)
        self._seed_canary_trades(mid, wins=2, losses=10)
        out = _run(evaluate_canaries(db, UID))
        assert out and out[0]["status"] == "rolled_back"
        assert out[0]["automatic"] is True
        m = _run(db.shadow_models.find_one({"user_id": UID}))
        assert m["status"] == "rolled_back"
        cfg = _run(db.bot_configs.find_one({"user_id": UID}))
        assert "trend" not in (cfg.get("engine_params_canary") or {})

    def test_healthy_canary_advances_after_stage_time(self):
        from canary_promotion import evaluate_canaries, start_canary
        db = _db()
        mid = _seed_model()
        _run(start_canary(db, UID, mid))
        # matching distribution: 8/12 wins ≈ 0.667, avg_r ≈ +0.67
        self._seed_canary_trades(mid, wins=8, losses=4)
        # backdate the stage start beyond MIN_STAGE_HOURS
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        _run(db.shadow_models.update_one(
            {"user_id": UID},
            {"$set": {"canary.stage_started_at": old,
                      "canary.started_at": old}}))
        # signals must predate... realized window = stage_started_at
        out = _run(evaluate_canaries(db, UID))
        assert out and out[0]["status"] == "canary"
        assert out[0]["allocation_pct"] == 10
        cfg = _run(db.bot_configs.find_one({"user_id": UID}))
        assert cfg["engine_params_canary"]["trend"]["allocation_pct"] == 10

    def test_final_stage_promotes_to_champion(self):
        from canary_promotion import evaluate_canaries, start_canary
        db = _db()
        mid = _seed_model()
        _run(start_canary(db, UID, mid))
        self._seed_canary_trades(mid, wins=8, losses=4)
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        _run(db.shadow_models.update_one(
            {"user_id": UID},
            {"$set": {"canary.stage_started_at": old,
                      "canary.stage_idx": 4,
                      "canary.allocation_pct": 100}}))
        out = _run(evaluate_canaries(db, UID))
        assert out and out[0]["status"] == "promoted"
        cfg = _run(db.bot_configs.find_one({"user_id": UID}))
        assert cfg["engine_params"]["trend"] == {"x": 1.0}
        assert "trend" not in (cfg.get("engine_params_canary") or {})

    def test_strategy_agent_draws_canary_params(self):
        """At 100% allocation every signal uses challenger params and is
        tagged; at 0% none are."""
        import asyncio
        from unittest.mock import AsyncMock, patch
        from agents.strategy_agent import StrategyAgent

        async def go(pct):
            cfg = {"engine_params": {"trend": {"x": 0.5}},
                   "engine_params_canary": {
                       "trend": {"params": {"x": 9.9},
                                 "allocation_pct": pct,
                                 "model_id": "m1", "version": "v1"}}}
            with patch("agents.strategy_agent.analyze_symbol",
                       new=AsyncMock(return_value={"symbol": "XAUUSD"})) \
                    as mock_an:
                sig = await StrategyAgent().propose("XAUUSD", "medium",
                                                    user_cfg=cfg)
                used = mock_an.call_args.kwargs["engine_params"]
                return sig, used
        sig, used = _run(go(100))
        assert used["trend"] == {"x": 9.9}
        assert sig["canary"]["model_id"] == "m1"
        sig, used = _run(go(0))
        assert used["trend"] == {"x": 0.5}
        assert "canary" not in sig


class TestDistributedRateLimit:
    def teardown_method(self):
        _cleanup()

    def test_mongo_token_bucket_enforces_limit(self):
        from distributed_rate_limit import allow_request
        db = _db()
        key = f"rltest-{uuid.uuid4().hex[:6]}"
        allowed = []
        for _ in range(8):
            ok, meta = _run(allow_request(
                db, key_id=key, tenant="t1", endpoint_class="read",
                limit_per_minute=5))
            allowed.append(ok)
        assert allowed[:5] == [True] * 5
        assert allowed[5:] == [False] * 3
        _run(db.rate_buckets.delete_many({"_id": {"$regex": key}}))

    def test_buckets_isolated_by_tenant_and_class(self):
        from distributed_rate_limit import allow_request
        db = _db()
        key = f"rltest-{uuid.uuid4().hex[:6]}"
        for _ in range(3):
            _run(allow_request(db, key_id=key, tenant="t1",
                               endpoint_class="write",
                               limit_per_minute=3))
        ok, _m = _run(allow_request(db, key_id=key, tenant="t1",
                                    endpoint_class="write",
                                    limit_per_minute=3))
        assert ok is False   # write bucket exhausted
        ok, _m = _run(allow_request(db, key_id=key, tenant="t1",
                                    endpoint_class="read",
                                    limit_per_minute=3))
        assert ok is True    # read bucket untouched
        ok, _m = _run(allow_request(db, key_id=key, tenant="t2",
                                    endpoint_class="write",
                                    limit_per_minute=3))
        assert ok is True    # other tenant untouched
        _run(db.rate_buckets.delete_many({"_id": {"$regex": key}}))

    def test_bucket_refills_over_time(self):
        from distributed_rate_limit import allow_request
        db = _db()
        key = f"rltest-{uuid.uuid4().hex[:6]}"
        for _ in range(2):
            _run(allow_request(db, key_id=key, tenant="t1",
                               endpoint_class="read", limit_per_minute=2))
        ok, _m = _run(allow_request(db, key_id=key, tenant="t1",
                                    endpoint_class="read",
                                    limit_per_minute=2))
        assert ok is False
        # backdate the bucket 60s → full refill
        _run(db.rate_buckets.update_one(
            {"_id": f"{key}:t1:read"},
            {"$inc": {"ts": -60_000}}))
        ok, _m = _run(allow_request(db, key_id=key, tenant="t1",
                                    endpoint_class="read",
                                    limit_per_minute=2))
        assert ok is True
        _run(db.rate_buckets.delete_many({"_id": {"$regex": key}}))
