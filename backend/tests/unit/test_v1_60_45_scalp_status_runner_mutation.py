"""v1.60.45 — GET /api/scalp/status raised `RuntimeError: dictionary changed size during iteration` when a runner was
added/removed by another coroutine while the status loop awaited a DB lookup. The loop now iterates a snapshot."""
import ast
import asyncio
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Runner:
    def __init__(self, acc, sym):
        self.account_id, self.symbol, self.user_id = acc, sym, "u1"
        self.enabled, self.mode, self.cfg, self.live_trades = True, "shadow", {}, []

    def status(self):
        return {"account_id": self.account_id, "symbol": self.symbol}


class _MutatingDb(FakeDb):
    """First awaited lookup inside the status loop adds a runner — the real-world race."""

    def __init__(self, runners):
        super().__init__()
        self._runners = runners
        self._fired = False

    def __getattr__(self, name):
        coll = super().__getattr__(name)
        if name == "scalp_tick_ingress" and not self._fired:
            self._fired = True
            orig = coll.find_one

            async def find_one(*a, **kw):
                self._runners["accZ:GBPUSD"] = _Runner("accZ", "GBPUSD")
                return await orig(*a, **kw)
            coll.find_one = find_one
        return coll


def test_status_survives_runner_added_during_iteration():
    import routes.scalp_routes as sr
    import scalp.engine as eng
    runners = {"accA:EURUSD": _Runner("accA", "EURUSD"), "accB:XAUUSD": _Runner("accB", "XAUUSD")}
    db = _MutatingDb(runners)
    with patch.object(sr, "get_db", lambda: db), patch.object(eng, "_runners", runners), \
            patch("scalp.permissions.maybe_refresh", lambda *a, **k: None):
        out = _run(sr.status(account_id=None, user={"id": "u1"}))
    assert "accZ:GBPUSD" in runners, "fixture must have mutated the dict mid-loop"
    assert len(out) >= 2


def test_status_loop_iterates_a_snapshot():
    src = open(os.path.join(ROOT, "backend", "routes", "scalp_routes.py")).read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "status")
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
    bad = [ast.unparse(n.iter) for n in loops if "_runners" in ast.unparse(n.iter)
           and not ast.unparse(n.iter).startswith("list(")]
    assert not bad, f"status() must iterate list(_runners...), found {bad}"
