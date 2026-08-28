"""v62.7 — Risk Truth hardening (pure unit tests, physically isolated).

Invariants:
  · RISK_UNKNOWN blocks NEW risk, never a safety exit (close/reduce).
  · Risk-truth read failures degrade to RISK_UNKNOWN — never exceptions,
    never accidental zeros.
  · Stale NAV is not drawdown evidence.
  · Stale latency evidence ⇒ Nitro/Fast-Scalp INELIGIBLE.
  · Decision snapshots are hashed and carry provenance.
"""
import asyncio
import inspect
import pathlib

import pytest

pytestmark = pytest.mark.unit


class TestRiskReducingClassifier:
    def test_close_and_reduce_signals(self):
        from modules.pamm.strategy_guard import is_risk_reducing
        assert is_risk_reducing({"action": "CLOSE"})
        assert is_risk_reducing({"action": "REDUCE"})
        assert is_risk_reducing({"action": "FLATTEN"})
        assert is_risk_reducing({"reduce_only": True, "action": "BUY"})
        assert is_risk_reducing({"close_trade": True})
        assert is_risk_reducing({"intent": "close"})
        assert is_risk_reducing({"pamm_risk_reducing": True})

    def test_new_exposure_signals_are_not(self):
        from modules.pamm.strategy_guard import is_risk_reducing
        assert not is_risk_reducing({"action": "BUY"})
        assert not is_risk_reducing({"action": "SELL"})
        assert not is_risk_reducing({})
        assert not is_risk_reducing({"reduce_only": False, "action": "BUY"})


class _BoomCursor:
    def __getattr__(self, k):
        raise RuntimeError("mongo timeout")


class _BoomCollection:
    def count_documents(self, *a, **k):
        raise RuntimeError("mongo timeout")

    def find(self, *a, **k):
        raise RuntimeError("mongo timeout")

    def find_one(self, *a, **k):
        raise RuntimeError("mongo timeout")


class _BoomDB:
    def __getattr__(self, k):
        return _BoomCollection()


class TestDbFailureBecomesRiskUnknown:
    def test_telemetry_survives_total_db_failure(self):
        """Mongo timeout during Risk Truth collection must yield
        evidence gaps (None) — not an exception, not zeros."""
        from modules.pamm.strategy_guard import (REQUIRED_TELEMETRY,
                                                 _telemetry,
                                                 missing_required_telemetry)
        program = {"program_id": "p_boom",
                   "last_nav": {"nav": 10000.0}}  # nav has NO 'at' → stale
        account = {"_id": "a_boom", "balance": 10000.0,
                   "current_spreads": {"EURUSD": 0.9}}
        t = asyncio.run(_telemetry(_BoomDB(), program, account,
                                   {"symbol": "EURUSD"}))
        missing = missing_required_telemetry(t)
        assert set(missing) == set(REQUIRED_TELEMETRY) - {"spread_pips"} \
            or set(missing) == set(REQUIRED_TELEMETRY), missing
        # never an accidental zero on failure
        for k in ("open_positions", "open_risk_pct_sum",
                  "daily_loss_pct", "weekly_loss_pct", "drawdown_pct"):
            assert t.get(k) is None, (k, t.get(k))

    def test_guard_wraps_collection_in_failsafe(self):
        from modules.pamm import strategy_guard as g
        src = inspect.getsource(g._authorize)
        assert "risk-truth collection failed entirely" in src
        assert "except Exception" in inspect.getsource(g._telemetry)


class TestStaleNavEvidence:
    def test_nav_without_timestamp_is_not_evidence(self):
        from modules.pamm.strategy_guard import _telemetry

        class _NavDB:
            def __getattr__(self, k):
                return _BoomCollection()

        t = asyncio.run(_telemetry(
            _NavDB(), {"program_id": "p1",
                       "last_nav": {"nav": 9000.0}},  # no 'at'
            {"_id": "a1"}, {"symbol": "EURUSD"}))
        assert t["drawdown_pct"] is None

    def test_nav_freshness_constant_and_wiring(self):
        from modules.pamm import strategy_guard as g
        assert g.NAV_FRESHNESS_S == 900
        src = inspect.getsource(g._telemetry)
        assert "nav_stale" in src and "NAV_FRESHNESS_S" in src

    def test_fresh_nav_produces_drawdown(self):
        from datetime import datetime, timezone

        from modules.pamm.strategy_guard import _telemetry

        class _Cursor:
            def __init__(self, docs):
                self._docs = list(docs)

            def limit(self, n):
                return self

            def sort(self, *a):
                return self

            def __aiter__(self):
                self._i = iter(self._docs)
                return self

            async def __anext__(self):
                try:
                    return next(self._i)
                except StopIteration:
                    raise StopAsyncIteration

        class _Trades:
            async def count_documents(self, q):
                return 0

            def find(self, *a, **k):
                return _Cursor([])

        class _Nav:
            def find(self, *a, **k):
                return _Cursor([{"nav": 10000.0}, {"nav": 9000.0}])

            async def find_one(self, *a, **k):
                return None

        class _DB:
            trades = _Trades()
            pamm_nav_snapshots = _Nav()

            def __getattr__(self, k):
                return _BoomCollection()

        now = datetime.now(timezone.utc).isoformat()
        t = asyncio.run(_telemetry(
            _DB(), {"program_id": "p1",
                    "last_nav": {"nav": 9000.0, "at": now}},
            {"_id": "a1", "balance": 10000.0}, {"symbol": "EURUSD"}))
        assert t["drawdown_pct"] == 10.0
        assert t["nav_age_s"] < 10


class TestNitroLatencyFreshness:
    def test_stale_latency_makes_nitro_ineligible(self):
        from strategies.execution_eligibility import apply_policy
        comps = {"spread_quality": 95, "latency_quality": 40.0,
                 "infrastructure_health": 95, "broker_quality": 90,
                 "slippage_quality": 90, "liquidity": 95,
                 "market_quality": 70, "regime_compatibility": 70}
        out = apply_policy("nitro_scalper", 85.0, comps)
        assert out["status"] == "INELIGIBLE"
        assert "latency_quality" in out["reason"]

    def test_fast_scalp_also_fails_on_stale_latency(self):
        from strategies.execution_eligibility import apply_policy
        comps = {"spread_quality": 95, "latency_quality": 40.0,
                 "infrastructure_health": 95}
        assert apply_policy("fast_scalp", 85.0,
                            comps)["status"] == "INELIGIBLE"

    def test_freshness_wired_into_evidence_collector(self):
        from strategies.nitro import eligibility as e
        assert e.LATENCY_EVIDENCE_MAX_AGE_S == 1800
        src = inspect.getsource(e.eligibility)
        assert "latency_evidence_age_s" in src
        assert "LATENCY_EVIDENCE_MAX_AGE_S" in src


class TestFailSafeAsymmetryWiring:
    def test_risk_reducing_bypasses_new_risk_blocks_only(self):
        from modules.pamm import strategy_guard as g
        src = inspect.getsource(g._authorize)
        assert "risk_reducing = is_risk_reducing(signal)" in src
        # every NEW-RISK block honours the bypass…
        assert "if not allowed and not risk_reducing:" in src
        assert 'pt.get("status") == "drift" and not risk_reducing' in src
        assert 'env_name == "LIVE" and not risk_reducing' in src
        assert "if canary_mode and not risk_reducing:" in src
        # …while the structural identity chain does NOT
        assert "strategy_provenance_missing" in src
        assert "governed_program_requires_assignment" in src


class TestSnapshotHashAndProvenance:
    def test_hash_is_deterministic_and_tamper_evident(self):
        from modules.pamm.strategy_guard import _snapshot_hash
        snap = {"snapshot_id": "rds_x", "reason": "authorized",
                "telemetry": {"spread_pips": 1.2}}
        h1 = _snapshot_hash(snap)
        assert h1 == _snapshot_hash(dict(snap))
        assert len(h1) == 64
        tampered = {**snap, "reason": "risk_unknown"}
        assert _snapshot_hash(tampered) != h1
        # the hash field itself never feeds the hash
        assert _snapshot_hash({**snap, "hash": h1}) == h1

    def test_snapshot_carries_provenance(self):
        from modules.pamm import strategy_guard as g
        src = inspect.getsource(g.authorize_pamm_strategy_execution)
        assert '"provenance"' in src
        assert "guard_version" in src
        assert "execution_policy_version" in src
        assert "ea_version" in src
        assert '_snapshot_hash' in src
        assert g.GUARD_VERSION == "v62.7"


class TestPhysicalCiIsolation:
    def test_ci_unit_lane_is_physically_scoped(self):
        ci = pathlib.Path(__file__).parents[4].joinpath(
            ".github/workflows/ci.yml").read_text()
        assert "pytest -m unit" not in ci
        assert "pytest tests/unit" in ci
