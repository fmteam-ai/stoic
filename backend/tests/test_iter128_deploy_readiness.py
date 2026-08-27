"""iter-128 — deployment readiness: health probe + ML kill-switches."""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
# The K8s health probe hits the BACKEND pod directly on its bind port
# (logs show "127.0.0.1:<port> - GET /health"), NOT through the /api ingress.
BACKEND = os.environ.get("BACKEND_INTERNAL_URL", "http://localhost:8001")


def test_root_health_returns_200_no_api_prefix():
    r = requests.get(f"{BACKEND}/health", timeout=20)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_healthz_alias():
    assert requests.get(f"{BACKEND}/healthz", timeout=20).status_code == 200


def test_health_has_no_db_dependency():
    # Root health must not touch the DB (readiness during startup / Atlas blip)
    import inspect
    import server
    src = inspect.getsource(server.root_health)
    assert "get_db" not in src and "db." not in src


def test_forecast_kill_switch_disables(monkeypatch):
    import asyncio
    import forecast_agent
    monkeypatch.setenv("FORECAST_AGENT_ENABLED", "false")

    async def go():
        # Must short-circuit to None WITHOUT importing torch/chronos.
        return await forecast_agent.get_forecast(None, "u1", "XAUUSD")
    assert asyncio.new_event_loop().run_until_complete(go()) is None


def test_ml_ensemble_kill_switch_disables(monkeypatch):
    import asyncio
    import ml_ensemble
    monkeypatch.setenv("ML_ENSEMBLE_ENABLED", "false")

    async def go():
        return await ml_ensemble.ml_predict(None, "u1", {"action": "BUY"},
                                            "XAUUSD")
    pred = asyncio.new_event_loop().run_until_complete(go())
    assert pred["p_win"] is None and pred["models_used"] == 0
    # a disabled prediction must never veto
    assert ml_ensemble.ml_gate(pred) is None


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
