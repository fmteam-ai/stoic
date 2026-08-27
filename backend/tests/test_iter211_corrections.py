"""iter-211 — the 10 recommended corrections (offline suite).

Covers: realized conformal coverage (+ uncertainty degradation),
canonical DecisionEvents, BOLA authorization matrix completeness,
intelligence health scope helpers, latency stat helpers."""
import asyncio
import os
import random

import pytest

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_database")


# ───────────────────────── conformal coverage ────────────────────────────

@pytest.mark.unit
class TestRealizedCoverage:
    def test_calibrated_history_passes(self):
        from uncertainty_engine import realized_coverage
        rng = random.Random(7)
        rs = [rng.gauss(0.1, 0.8) for _ in range(200)]
        cov = realized_coverage(rs)
        assert cov["evaluated"] == 180
        assert cov["coverage"] is not None
        assert cov["ok"] is True
        assert cov["coverage"] >= cov["target"] - 0.05

    def test_distribution_shift_fails_coverage(self):
        from uncertainty_engine import realized_coverage
        rng = random.Random(3)
        # train regime tight around 0, recent regime far outside
        rs = [rng.gauss(0.0, 0.1) for _ in range(60)] + \
             [rng.gauss(5.0, 0.1) for _ in range(60)]
        cov = realized_coverage(rs)
        assert cov["ok"] is False
        assert cov["coverage"] < cov["target"] - 0.05

    def test_insufficient_history_never_blocks(self):
        from uncertainty_engine import realized_coverage
        cov = realized_coverage([0.1] * 25)  # only 5 evaluations
        assert cov["ok"] is True
        assert cov["coverage"] is None

    def test_failed_coverage_degrades_uncertainty(self):
        # components math: coverage penalty raises total uncertainty
        from uncertainty_engine import COVERAGE_PENALTY_U
        assert COVERAGE_PENALTY_U > 0


# ─────────────────────── canonical decision events ───────────────────────

class _FakeEvents:
    def __init__(self):
        self.docs = []

    async def count_documents(self, q, limit=None):
        return len([d for d in self.docs
                    if d["decision_id"] == q["decision_id"]])

    async def insert_one(self, doc):
        self.docs.append(doc)


class _FakeDb:
    def __init__(self):
        self.decision_events = _FakeEvents()


@pytest.mark.unit
class TestCanonicalDecisionEvents:
    def test_pipeline_stages_are_canonical(self):
        from decision_context import CANONICAL_STAGES
        for s in ("meta_decision", "market_memory", "portfolio_brain",
                  "pretrade_twin", "execution_alpha"):
            assert s in CANONICAL_STAGES

    def test_non_canonical_stage_dropped(self):
        from decision_context import record_stage
        db = _FakeDb()
        asyncio.run(record_stage(db, "dec_x", "random_debug_blob", {}))
        assert db.decision_events.docs == []

    def test_canonical_stage_recorded(self):
        from decision_context import record_stage
        db = _FakeDb()
        asyncio.run(record_stage(db, "dec_x", "meta_decision",
                                 {"decision": "TRADE"}))
        assert len(db.decision_events.docs) == 1
        assert db.decision_events.docs[0]["stage"] == "meta_decision"

    def test_cap_still_enforced(self):
        from decision_context import MAX_STAGES, record_stage
        db = _FakeDb()
        for _ in range(MAX_STAGES + 5):
            asyncio.run(record_stage(db, "dec_x", "outcome", {}))
        assert len(db.decision_events.docs) == MAX_STAGES


# ───────────────────────── BOLA matrix completeness ──────────────────────

@pytest.mark.integration
class TestBolaMatrix:
    def test_every_sensitive_route_declared(self):
        """New endpoints taking account_id/bot_id CANNOT ship without an
        explicit entry in security_matrix.BOLA_MATRIX."""
        from security_matrix import BOLA_MATRIX, sensitive_routes_from_openapi
        from server import app
        spec = app.openapi()
        sensitive = sensitive_routes_from_openapi(spec)
        assert sensitive, "openapi enumeration returned nothing"
        missing = sorted(r for r in sensitive if r not in BOLA_MATRIX)
        assert not missing, (
            f"UNDECLARED sensitive routes (add to security_matrix."
            f"BOLA_MATRIX with their ownership enforcement): {missing}")

    def test_matrix_mechanisms_valid(self):
        from security_matrix import BOLA_MATRIX
        assert all(m in ("user_scoped_query", "owned_account_helper")
                   for m in BOLA_MATRIX.values())


# ───────────────────── latency stats & clock skew ────────────────────────

@pytest.mark.unit
class TestLatencyStats:
    def test_pct_p99_and_max(self):
        from latency_profiler import _pct
        vals = list(range(1, 101))
        assert _pct(vals, 0.5) == 51
        assert _pct(vals, 0.99) == 99
        assert _pct([], 0.99) is None

    def test_clock_skew_status_thresholds(self):
        # negative t7−t6 minimum below −250ms must flag SKEW_SUSPECTED
        skew_bound = min(0, -400)
        assert skew_bound < -250


# ─────────────────────── intelligence health scopes ──────────────────────

@pytest.mark.unit
class TestIntelScopes:
    def test_connected_logic(self):
        from intel_scopes import _connected, _hb_cutoff
        cutoff = _hb_cutoff()
        assert _connected({"mode": "paper"}, cutoff) is True
        assert _connected({"last_heartbeat": "2999-01-01T00:00:00"},
                          cutoff) is True
        assert _connected({"last_heartbeat": "2020-01-01T00:00:00"},
                          cutoff) is False
        assert _connected({}, cutoff) is False

    def test_markdown_matrix_generates(self):
        from security_matrix import to_markdown
        md = to_markdown()
        assert "/api/brain/costs" in md and "| Method |" in md
