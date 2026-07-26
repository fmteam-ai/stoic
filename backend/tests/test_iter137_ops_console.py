"""iter-137 — Ops Console aggregation endpoint + latency ring buffer."""
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def test_ops_metrics_summary():
    import ops_metrics
    ops_metrics._SAMPLES.clear()
    for ms in (10, 20, 30, 40, 1000):
        ops_metrics.record(ms, 200)
    ops_metrics.record(50, 500)
    s = ops_metrics.summary()
    assert s["count"] == 6
    assert s["p50_ms"] <= s["p95_ms"] <= s["max_ms"] == 1000
    assert s["error_rate_pct"] == pytest.approx(16.67, abs=0.1)


def test_ops_console_admin_only():
    from routes.ops_console import ops_console
    with pytest.raises(HTTPException) as e:
        _run(ops_console(user={"role": "user"}))
    assert e.value.status_code == 403


def test_ops_console_shape():
    from routes.ops_console import ops_console
    out = _run(ops_console(user={"role": "admin", "two_factor_enabled": True}))
    for key in ("vps", "deployments", "command_queue", "mt5_bridge",
                "engine", "alerts", "api", "mongo", "stripe",
                "subscriptions", "generated_at"):
        assert key in out, key
    assert out["mongo"]["ok"] is True
    assert out["vps"]["agents_total"] >= out["vps"]["online"]
    assert isinstance(out["alerts"]["unacked_total"], int)
    assert isinstance(out["subscriptions"]["by_plan"], dict)
    assert out["mt5_bridge"]["accounts_total"] == (
        out["mt5_bridge"]["connected"] + out["mt5_bridge"]["disconnected"])


def test_stripe_webhook_feed_write_is_wired():
    import inspect
    from routes import subscription_routes
    src = inspect.getsource(subscription_routes.stripe_webhook)
    assert "stripe_webhook_events" in src
