"""Phase F — worker architecture: protection / analytics / model services
separated, DB-only task modules. Unit portion is MongoDB-free."""
import inspect

import pytest

pytestmark = pytest.mark.unit


class TestServiceSeparation:
    def test_worker_entrypoints_exist(self):
        for name, loop in (("protection", "_protection_guard_loop"),
                           ("analytics", "_analytics_loop"),
                           ("model", "_model_maintenance_loop")):
            src = open(f"/app/backend/workers/{name}.py").read()
            assert loop in src
            assert "workers.base import main" in src

    def test_loops_defined_in_background_loops(self):
        src = open("/app/backend/background_loops.py").read()
        for loop in ("_protection_guard_loop", "_analytics_loop",
                     "_model_maintenance_loop"):
            assert f"async def {loop}(" in src

    def test_protection_extracted_from_reconcile(self):
        import background_loops
        rec = inspect.getsource(background_loops._scalp_reconcile_loop)
        assert "repair_unprotected_positions" not in rec
        prot = inspect.getsource(background_loops._protection_guard_loop)
        assert "repair_unprotected_positions" in prot

    def test_server_runs_all_in_process(self):
        src = open("/app/backend/server.py").read()
        for loop in ("_protection_guard_loop", "_analytics_loop",
                     "_model_maintenance_loop"):
            assert f"asyncio.create_task({loop}())" in src
        # shutdown cancels them
        assert "_protection_task, _analytics_task, _model_maint_task" in src

    def test_architecture_documented(self):
        doc = open("/app/backend/workers/README.md").read()
        for svc in ("Protection worker", "Analytics worker", "Model worker",
                    "Broker Gateway", "BACKGROUND_WORKERS_IN_PROCESS"):
            assert svc in doc


class TestTaskModulesAreDbOnly:
    def test_analytics_has_no_runner_access(self):
        src = open("/app/backend/analytics_tasks.py").read()
        assert "_runners" not in src
        assert "from scalp.engine" not in src

    def test_model_tasks_have_no_runner_access(self):
        src = open("/app/backend/model_tasks.py").read()
        assert "_runners" not in src
        assert "from scalp.engine" not in src


class TestModelMaintenance:
    @pytest.mark.asyncio
    async def test_skips_thin_and_malformed_keys(self):
        from unittest.mock import AsyncMock, MagicMock
        import model_tasks
        db = MagicMock()
        db.scalp_decisions.distinct = AsyncMock(
            return_value=["broker|live|EURUSD", "not-a-key"])
        db.scalp_decisions.count_documents = AsyncMock(return_value=5)
        db.scalp_model_audit.insert_one = AsyncMock()
        out = await model_tasks.run_model_maintenance(db, min_resolved=200)
        assert out["retrained"] == []
        assert out["skipped"] == 2
        db.scalp_model_audit.insert_one.assert_not_awaited()


class TestAnalyticsDayMath:
    def test_day_start_ms(self):
        from analytics_tasks import _day_start_ms
        assert _day_start_ms("1970-01-02") == 86_400_000
