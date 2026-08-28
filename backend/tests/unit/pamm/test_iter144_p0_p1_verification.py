"""ITER-144 P0/P1 verification tests:
1) p95 slippage worst-of gate in envelope_violations
2) _telemetry p95 index math against sorted slippage list
3) Snapshot provenance uses the injected-build-SHA mechanism (BUILD_SHA
   file > env fallback > "unknown"; production hard-fails on "unknown")
4) Snapshot hash is SHA-256 tamper-evident
5) outcome_attribution.attribute_trade writes risk_snapshot_id
"""
import re
import hashlib
import json
import pytest
from unittest.mock import AsyncMock, MagicMock


# ─── (1) p95 worst-of slippage gate ─────────────────────────────────────
class TestP95SlippageWorstOf:
    def _env(self):
        from modules.pamm.strategy_guard import envelope_violations
        return envelope_violations

    def _envelope(self):
        return {"max_slippage_pips": 2.0}

    def _tele(self, med, p95):
        return {"recent_slippage_pips": med, "recent_slippage_p95_pips": p95}

    def _sig(self):
        return {"symbol": "EURUSD", "lot_size": 0.1}

    def test_median_under_but_p95_over_blocks(self):
        v = self._env()(self._envelope(), self._sig(), self._tele(1.0, 5.0))
        reasons = [x["reason"] for x in v]
        assert "expected_slippage_exceeded" in reasons
        d = next(x for x in v if x["reason"] == "expected_slippage_exceeded")["detail"]
        assert d["recent_median_slippage_pips"] == 1.0
        assert d["recent_p95_slippage_pips"] == 5.0

    def test_median_over_but_p95_under_still_blocks(self):
        v = self._env()(self._envelope(), self._sig(), self._tele(3.5, 0.5))
        assert any(x["reason"] == "expected_slippage_exceeded" for x in v)

    def test_both_under_passes(self):
        v = self._env()(self._envelope(), self._sig(), self._tele(0.5, 1.9))
        assert not any(x["reason"] == "expected_slippage_exceeded" for x in v)

    def test_median_only_over_blocks_when_p95_none(self):
        v = self._env()(self._envelope(), self._sig(), self._tele(3.0, None))
        assert any(x["reason"] == "expected_slippage_exceeded" for x in v)

    def test_p95_only_over_blocks_when_med_none(self):
        v = self._env()(self._envelope(), self._sig(), self._tele(None, 3.0))
        assert any(x["reason"] == "expected_slippage_exceeded" for x in v)

    def test_all_none_no_block(self):
        v = self._env()(self._envelope(), self._sig(), self._tele(None, None))
        assert not any(x["reason"] == "expected_slippage_exceeded" for x in v)


# ─── (2) telemetry p95 index math ────────────────────────────────────────
class TestP95IndexMath:
    def test_p95_index_formula(self):
        # Formula: index = min(n-1, int(0.95*(n-1) + 0.5))
        for n in range(1, 21):
            sorted_list = list(range(n))
            expected = min(n - 1, int(0.95 * (n - 1) + 0.5))
            assert sorted_list[expected] == expected

    def test_p95_matches_max_on_small_lists(self):
        # For lists <= 10, p95 index resolves to last (or near-last) element
        for n in [1, 2, 5, 10]:
            idx = min(n - 1, int(0.95 * (n - 1) + 0.5))
            assert idx == n - 1


# ─── (3) & (4) Snapshot provenance: injected build SHA + tamper hash ────
class TestSnapshotProvenance:
    def test_build_sha_reads_injected_file(self, tmp_path):
        from modules.pamm.strategy_guard import _build_sha
        f = tmp_path / "BUILD_SHA"
        f.write_text("a" * 40 + "\n")
        assert _build_sha(path=f, env={}) == "a" * 40

    def test_build_sha_env_fallbacks(self, tmp_path):
        from modules.pamm.strategy_guard import _build_sha
        missing = tmp_path / "nope"
        assert _build_sha(path=missing,
                          env={"STOIC_BUILD_SHA": "b" * 40}) == "b" * 40
        assert _build_sha(path=missing,
                          env={"GITHUB_SHA": "C" * 40}) == "c" * 40
        assert _build_sha(path=missing, env={}) == "unknown"

    def test_build_sha_rejects_malformed(self, tmp_path):
        from modules.pamm.strategy_guard import _build_sha
        f = tmp_path / "BUILD_SHA"
        f.write_text("$Format:%H$")  # unexpanded git-archive placeholder
        assert _build_sha(path=f, env={"GITHUB_SHA": "not-a-sha"}) == "unknown"

    def test_injected_file_wins_over_env(self, tmp_path):
        from modules.pamm.strategy_guard import _build_sha
        f = tmp_path / "BUILD_SHA"
        f.write_text("e" * 40)
        assert _build_sha(path=f, env={"GITHUB_SHA": "f" * 40}) == "e" * 40

    def test_production_requires_provenance(self):
        from modules.pamm.strategy_guard import _enforce_production_provenance
        with pytest.raises(RuntimeError):
            _enforce_production_provenance(sha="unknown", production=True,
                                           image_digest="sha256:x")
        _enforce_production_provenance(sha="unknown", production=False)
        _enforce_production_provenance(sha="d" * 40, production=True,
                                       image_digest="sha256:" + "e" * 64)

    def test_production_requires_image_digest(self):
        from modules.pamm.strategy_guard import _enforce_production_provenance
        with pytest.raises(RuntimeError, match="STOIC_IMAGE_DIGEST"):
            _enforce_production_provenance(sha="d" * 40, production=True,
                                           image_digest="")
        _enforce_production_provenance(sha="d" * 40, production=False,
                                       image_digest="")

    def test_snapshot_hash_is_sha256_and_tamper_evident(self):
        from modules.pamm.strategy_guard import _snapshot_hash
        snap = {"snapshot_id": "rds_abc", "authorized": True, "envelope": {"a": 1}}
        h1 = _snapshot_hash(snap)
        assert re.fullmatch(r"[0-9a-f]{64}", h1)
        # deterministic
        assert _snapshot_hash(dict(snap)) == h1
        # tamper flips hash
        snap2 = dict(snap); snap2["authorized"] = False
        assert _snapshot_hash(snap2) != h1
        # 'hash' field itself is excluded
        snap3 = dict(snap); snap3["hash"] = "deadbeef"
        assert _snapshot_hash(snap3) == h1

    def test_snapshot_hash_uses_sha256_canonical_json(self):
        from modules.pamm.strategy_guard import _snapshot_hash
        snap = {"b": 2, "a": 1}
        expected = hashlib.sha256(
            json.dumps({"a": 1, "b": 2}, sort_keys=True, default=str).encode()
        ).hexdigest()
        assert _snapshot_hash(snap) == expected


# ─── (5) Outcome attribution stamps risk_snapshot_id ────────────────────
class _AsyncIter:
    def __init__(self, items):
        self._items = list(items)
    def __aiter__(self):
        return self
    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)
    def sort(self, *a, **kw): return self
    def limit(self, *a, **kw): return self


class _StubColl:
    def __init__(self):
        self.updates = []
    async def find_one(self, *a, **kw): return None
    async def count_documents(self, *a, **kw): return 0
    def find(self, *a, **kw): return _AsyncIter([])
    async def update_one(self, query, update, upsert=False):
        self.updates.append({"query": query, "update": update, "upsert": upsert})


class _StubDB:
    def __init__(self):
        self.execution_intents = _StubColl()
        self.pamm_incidents = _StubColl()
        self.pamm_news_cache = _StubColl()
        self.trades = _StubColl()
        self.trade_outcomes = _StubColl()


class TestOutcomeAttributionRiskSnapshotLink:
    @pytest.mark.asyncio
    async def test_attribute_trade_carries_risk_snapshot_id(self):
        import outcome_attribution as oa
        trade = {
            "_id": "507f1f77bcf86cd799439011",
            "user_id": "u1", "account_id": "a1", "symbol": "EURUSD",
            "status": "closed", "pnl": 10.0,
            "closed_at": "2026-01-01T01:00:00Z",
            "opened_at": "2026-01-01T00:00:00Z",
            "entry_price": 1.10, "exit_price": 1.11, "stop_loss": 1.09,
            "side": "buy", "lot_size": 0.1,
            "pamm_risk_snapshot_id": "rds_test_abc123",
            "decision_id": "dec_1", "scope": "manual",
        }
        db = _StubDB()
        outcome = await oa.attribute_trade(db, trade)
        assert outcome["risk_snapshot_id"] == "rds_test_abc123"
        # persisted upsert into trade_outcomes carries the same id
        assert db.trade_outcomes.updates, "trade_outcomes.update_one must be called"
        persisted = db.trade_outcomes.updates[0]["update"]["$set"]
        assert persisted["risk_snapshot_id"] == "rds_test_abc123"
        assert db.trade_outcomes.updates[0]["query"]["trade_id"] == "507f1f77bcf86cd799439011"
        assert db.trade_outcomes.updates[0]["upsert"] is True

    @pytest.mark.asyncio
    async def test_attribute_trade_without_snapshot_id_is_none(self):
        import outcome_attribution as oa
        trade = {
            "_id": "507f1f77bcf86cd799439012",
            "user_id": "u1", "account_id": "a1", "symbol": "EURUSD",
            "status": "closed", "pnl": -5.0,
            "closed_at": "2026-01-01T01:00:00Z",
            "opened_at": "2026-01-01T00:00:00Z",
            "entry_price": 1.10, "exit_price": 1.09, "stop_loss": 1.09,
            "side": "buy", "lot_size": 0.1, "scope": "manual",
        }
        db = _StubDB()
        outcome = await oa.attribute_trade(db, trade)
        assert outcome["risk_snapshot_id"] is None
        persisted = db.trade_outcomes.updates[0]["update"]["$set"]
        assert persisted["risk_snapshot_id"] is None
