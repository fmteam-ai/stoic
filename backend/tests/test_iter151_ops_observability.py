"""iter-151 · Ops observability + Risk-Commander proposal gate:
safety-status banner endpoint, account certification, execution latency /
counters / infra probes, and the AI→proposal→operator-approval flow."""
import os as _os
import asyncio
import requests
from live_target import require_live_base_url
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)

BASE = require_live_base_url()


def _login():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=15)
    assert r.status_code == 200, r.text
    return s


def test_safety_status_shape():
    s = _login()
    r = s.get(f"{BASE}/api/bot/safety-status", timeout=20)
    assert r.status_code == 200, r.text
    j = r.json()
    for k in ("level", "live_accounts", "unprotected_open",
              "unresolved_submissions", "stale_feeds", "tripped",
              "daily_worst_consumed_pct"):
        assert k in j, f"safety-status missing {k}"
    assert j["level"] in ("safe", "warn", "critical")


def test_accounts_certification_shape():
    s = _login()
    r = s.get(f"{BASE}/api/accounts/certification", timeout=20)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert isinstance(items, list) and items, "expected at least one account"
    keys = {c["key"] for c in items[0]["checks"]}
    for k in ("ea_version", "bridge_paired", "heartbeat", "account_type",
              "symbol_specs", "spread_feed", "clock_sync", "history_sync",
              "identity"):
        assert k in keys, f"certification missing check {k}"
    assert "certified" in items[0] and "passed" in items[0]


def test_execution_health_sre_sections():
    s = _login()
    r = s.get(f"{BASE}/api/bot/execution-health", timeout=25)
    assert r.status_code == 200, r.text
    j = r.json()
    lat = j["latency"]
    for k in ("n", "p50_sec", "p95_sec", "p99_sec"):
        assert k in lat
    for k in ("rejects_24h", "replays_24h", "partial_fills_open"):
        assert k in j["counters"]
    assert j["infra"]["mongo_latency_ms"] is not None
    assert isinstance(j["infra"]["heartbeats"], list)


# ------------------- Risk Commander: AI → proposal → operator approval
def test_nl_confirm_rejects_unknown_actions():
    s = _login()
    r = s.post(f"{BASE}/api/nl/command/confirm",
               json={"actions": [{"type": "DELETE_EVERYTHING"}]}, timeout=15)
    assert r.status_code == 400


def test_nl_confirm_requires_actions():
    s = _login()
    r = s.post(f"{BASE}/api/nl/command/confirm", json={}, timeout=15)
    assert r.status_code == 400


def test_sensitive_gate_in_source():
    src = open(_os.path.join(_BACKEND_DIR, "routes", "nl_routes.py")).read()
    assert 'SENSITIVE_NL_ACTIONS = {"SET_RISK_LEVEL", "ENABLE_BOTS", "CLOSE_ALL_TRADES"}' in src
    assert '"requires_confirmation": True' in src
    # the gate must sit BEFORE execution in nl_command
    cmd = src[src.index("async def nl_command("):src.index("async def nl_command_confirm(")] \
        if src.index("async def nl_command(") < src.index("async def nl_command_confirm(") \
        else src[src.index("async def nl_command("):]
    assert cmd.index("requires_confirmation") < cmd.index("_execute_actions")
    # raw interpret exception no longer leaks
    assert 'detail=f"AI interpret failed: {e}"' not in src


def test_deployment_artifacts_exist():
    for p in ("Dockerfile.backend", "Dockerfile.frontend",
              "docker-compose.yml", ".github/workflows/ci.yml",
              "scripts/check_ea_structure.py",
              "backend/.env.example", "frontend/.env.example",
              "frontend/yarn.lock"):
        assert _os.path.exists(_os.path.join(_REPO_DIR, p)), f"missing {p}"


def test_ea_structural_ci_script_passes():
    import subprocess
    r = subprocess.run(
        ["python", _os.path.join(_REPO_DIR, "scripts", "check_ea_structure.py")],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
