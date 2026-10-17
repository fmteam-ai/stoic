"""v1.60.39 — background safety loops must have an owner in external-workers mode (production).

13 Oct: the execution-intent sweep ran only in server.py's in-process block. 14 Oct: same for
scheduled_drill_loop (deploy bake watch / auto-rollback, candidate auto-promotion, audit anchor, drills) — the
19:18 promotion's deploy_watch stayed pending forever. This test fails the moment a loop is added to server.py's
in-process block without a workers/* owner."""
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
BACKEND = os.path.join(ROOT, "backend")


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def in_process_loops() -> dict:
    """{loop_callable_name: canonical_name} for every create_task(...) in server.py's in-process block."""
    src = _read("backend", "server.py")
    gate = src.index('BACKGROUND_WORKERS_IN_PROCESS')
    block = src[gate:]
    block = block[:block.index("\n\n\n")] if "\n\n\n" in block else block
    # alias → "module.real" for `from module import real as alias`
    alias_to_real = {a: f"{m}.{r}" for m, r, a in re.findall(r"from\s+([\w.]+)\s+import\s+(\w+)\s+as\s+(\w+)", block)}
    found = {}
    for name in re.findall(r"create_task\(([\w.]+)\(\)\)", block):
        found[name] = alias_to_real.get(name, name)
    return found


def worker_identifiers() -> set:
    """Identifiers referenced in CODE (not docstrings/comments) across workers/*.py: names, attributes, imports."""
    import ast
    ids: set = set()
    wdir = os.path.join(BACKEND, "workers")
    for fn in sorted(os.listdir(wdir)):
        if not fn.endswith(".py") or fn in ("base.py", "__init__.py"):
            continue
        tree = ast.parse(open(os.path.join(wdir, fn), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                ids.add(node.id)
            elif isinstance(node, ast.Attribute):
                ids.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                ids.add(node.module or "")
                ids.add((node.module or "").split(".")[-1])
                for a in node.names:
                    ids.add(a.name)
                    ids.add(a.asname or a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    ids.add(a.name)
                    ids.add(a.name.split(".")[-1])
    return ids


def test_every_in_process_loop_has_a_worker_owner():
    loops = in_process_loops()
    assert len(loops) >= 25, loops                                            # sanity: the block was actually parsed
    workers = worker_identifiers()
    missing = []
    for name, real in loops.items():
        # a worker owns the loop if it references the alias, the real function name, or the owning module
        # (e.g. `bot_runner.loop` / `from security_agent.runner import loop`)
        candidates = {name, real}
        if "." in real:
            mod, fn = real.rsplit(".", 1)
            candidates |= {mod, mod.split(".")[-1]} if fn == "loop" else {fn}
        if not candidates & workers:
            missing.append(name)
    assert not missing, f"loops started only in-process (never in production): {missing}"


def test_the_previously_orphaned_loops_now_run_in_the_reconciliation_worker():
    w = _read("backend", "workers", "reconciliation.py")
    for name in ("scheduled_drill_loop", "_deploy_watch_loop", "_heartbeat_watch_loop", "_mode_guardian_loop",
                 "_soak_sampler_loop", "_soak_tracker_loop", "_billing_loop", "_ops_outbox_loop", "_broker_intel_loop"):
        assert name in w, name
    sd = _read("backend", "scheduled_drills.py")
    assert 'record_progress("scheduled_drill_loop"' in sd and 'persist_progress(db, "scheduled_drill_loop")' in sd


def test_stale_deploy_watch_is_resolved_clean_without_rollback():
    import deployment_health as dh
    db = FakeDb()
    started = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()      # 14 Oct 19:18 → bake 4 h, long over
    db.platform_state.rows.append({"_id": "release_state", "stable": {"stoic-ea": "b" * 64},
                                   "deploy_watch": {"started_at": started, "bake_hours": 4, "baseline_score": 100,
                                                    "artifacts": {"stoic-ea": "b" * 64}}})
    rollback = AsyncMock()
    with patch.object(dh, "_trusted_user_ids", AsyncMock(return_value={"u1"})), \
            patch.object(dh, "distinct_fail_tenants", AsyncMock(return_value=0)), \
            patch.object(dh, "score_fleet", AsyncMock(return_value={"score": 20.0, "fleet_size": 3})), \
            patch("release_channels.rollback", rollback):
        out = _run(dh.watch_deployment(db))
    assert out["status"] == "bake_complete" and out["stale_resolved"] is True and out["fail_tenants"] == 0
    rollback.assert_not_awaited()                                                # today's degraded fleet is irrelevant
    assert "deploy_watch" not in (db.platform_state.rows[0]) or not db.platform_state.rows[0].get("deploy_watch")
    ev = [h for h in db.release_history.rows if h["event"] == "bake_complete"][0]["detail"]
    assert ev["stale_resolved"] is True and ev["fail_tenants"] == 0 and ev["elapsed_hours"] >= 29


def test_fresh_watch_inside_bake_window_still_uses_live_health():
    import deployment_health as dh
    db = FakeDb()
    started = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.platform_state.rows.append({"_id": "release_state", "deploy_watch": {"started_at": started, "bake_hours": 4,
                                                                             "baseline_score": 100, "artifacts": {}}})
    with patch.object(dh, "_trusted_user_ids", AsyncMock(return_value=set())), \
            patch.object(dh, "distinct_fail_tenants", AsyncMock(return_value=0)), \
            patch.object(dh, "score_fleet", AsyncMock(return_value={"score": 95.0, "fleet_size": 0})):
        out = _run(dh.watch_deployment(db))
    assert out["status"] == "baking"


def test_bot_health_shows_loop_last_run_times():
    assert '@router.get("/ops/loops")' in _read("backend", "routes", "ops_routes.py")
    card = _read("frontend", "src", "components", "SafetyLoopsCard.jsx")
    assert 'api.get("/ops/loops")' in card and "last_run_at" in _read("backend", "routes", "ops_routes.py")
    assert 'user?.role === "admin" && <SafetyLoopsCard />' in _read("frontend", "src", "pages", "BotHealth.jsx")
