"""iter-158 — DR/rollback drills, Stress Test mode, SLOs, correlation IDs,
broker capability profiles, deployment-status alerts."""
import os
import sys

import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"
TIMEOUT = 90


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _admin():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


# ─── DR & rollback drills (inside the chaos campaign) ────────────────
def test_chaos_campaign_includes_dr_drills_and_passes():
    from chaos_drills import run_drills
    out = _run(run_drills(_db()))
    names = {r["drill"] for r in out["results"]}
    for expected in ("config_rollback", "artifact_rollback",
                     "panic_recovery", "backup_restore"):
        assert expected in names, f"missing DR drill {expected}"
    failed = [r for r in out["results"] if not r["passed"]]
    assert not failed, f"chaos/DR drill failures: {failed}"
    assert out["total"] == 16  # 12 + 4 security drills (iter-161)


# ─── stress test mode ────────────────────────────────────────────────
def test_stress_test_all_severities_stay_calm():
    from stress_test import run_stress_test, SEVERITIES
    db = _db()
    for sev in SEVERITIES:
        out = _run(run_stress_test(db, severity=sev, actor="pytest"))
        assert out["verdict"] == "STAYED_CALM", (
            f"{sev}: {[c for c in out['checks'] if c['status'] != 'pass']}")
        layers = {c["layer"] for c in out["checks"]}
        assert layers == {"volatility_shock_filter", "spread_guard",
                          "drawdown_circuit_breaker", "position_sizing_floor",
                          "post_crash_recovery"}


def test_stress_test_http_endpoints():
    r = requests.post(f"{API}/ops/stress-test/run?severity=moderate",
                      timeout=TIMEOUT)
    assert r.status_code in (401, 403)  # admin only
    s = _admin()
    r = s.post(f"{API}/ops/stress-test/run?severity=bogus", timeout=TIMEOUT)
    assert r.status_code == 400
    r = s.post(f"{API}/ops/stress-test/run?severity=severe", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "STAYED_CALM" and body["severity"] == "severe"
    r = s.get(f"{API}/ops/stress-test/runs", timeout=TIMEOUT)
    assert r.status_code == 200
    assert r.json()["runs"][0]["run_id"] == body["run_id"]


# ─── SLOs / error budgets ────────────────────────────────────────────
def test_slo_endpoint():
    s = _admin()
    r = s.get(f"{API}/ops/slo", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    slos = r.json()["slos"]
    assert set(slos) == {"api_latency", "api_availability",
                         "heartbeat_freshness", "deployment_success"}
    for v in slos.values():
        assert v["status"] in ("ok", "at_risk", "breached", "no_data")
        assert "target_pct" in v and "budget_consumed_pct" in v
    # traffic exists in this environment — API SLOs must have data
    assert slos["api_availability"]["total"] > 0
    r2 = requests.get(f"{API}/ops/slo", timeout=TIMEOUT)
    assert r2.status_code in (401, 403)


# ─── correlation IDs ─────────────────────────────────────────────────
def test_response_carries_request_id_header():
    rid = "iter158-trace-abc123"
    r = requests.get(f"{API}/health", headers={"X-Request-ID": rid},
                     timeout=TIMEOUT)
    assert r.headers.get("X-Request-ID") == rid


def test_correlation_filter_injects_rid():
    import logging
    from correlation import (CorrelationFilter, set_correlation_id,
                             new_correlation_id, get_correlation_id)
    set_correlation_id("test-rid-99")
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "msg", None, None)
    CorrelationFilter().filter(rec)
    assert rec.rid == "test-rid-99"
    rid = new_correlation_id(prefix="wrk-test-")
    assert rid.startswith("wrk-test-") and get_correlation_id() == rid


# ─── broker capability profiles ──────────────────────────────────────
def test_capabilities_defaults_and_overrides():
    from broker_registry import (DEFAULT_CAPABILITIES, merged_capabilities,
                                 capabilities_for)
    assert merged_capabilities(None) == DEFAULT_CAPABILITIES
    merged = merged_capabilities(
        {"capabilities": {"position_mode": "netting", "bogus_key": 1}})
    assert merged["position_mode"] == "netting"
    assert "bogus_key" not in merged
    assert merged["fill_policy"] == DEFAULT_CAPABILITIES["fill_policy"]
    caps = _run(capabilities_for(_db(), "TotallyUnknownBroker-Live"))
    assert caps == DEFAULT_CAPABILITIES


def test_resolved_registry_entry_carries_capabilities():
    from broker_registry import resolve_registry, invalidate_cache
    invalidate_cache()
    entry = _run(resolve_registry(_db(), "Exness-MT5Real8"))
    assert entry is not None and "capabilities" in entry
    assert entry["capabilities"]["position_mode"] in ("hedging", "netting")


def test_admin_broker_update_accepts_capabilities():
    s = _admin()
    r = s.get(f"{API}/admin/brokers", timeout=TIMEOUT)
    assert r.status_code == 200
    b = r.json()[0]
    payload = {**{k: b[k] for k in ("broker_id", "name", "server_aliases",
                                    "symbol_map", "contract_specs",
                                    "stop_level_points",
                                    "freeze_level_points", "sessions")},
               "capabilities": {"position_mode": "netting", "junk": True}}
    r = s.put(f"{API}/admin/brokers/{b['broker_id']}", json=payload, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    caps = r.json().get("capabilities") or {}
    assert caps.get("position_mode") == "netting" and "junk" not in caps
    # restore
    payload["capabilities"] = b.get("capabilities") or {}
    assert s.put(f"{API}/admin/brokers/{b['broker_id']}", json=payload,
                 timeout=TIMEOUT).status_code == 200


# ─── deployment status + centralized alert ───────────────────────────
def test_deploy_status_records_and_alerts_on_failure():
    from vps_agent import agent_by_token  # noqa: F401 — ensures module loads
    db = _db()
    token = f"agt_tok_iter158-{os.urandom(6).hex()}"
    agent_id = f"iter158-agent-{os.urandom(4).hex()}"

    async def seed():
        await db.vps_agents.insert_one(
            {"agent_id": agent_id, "agent_token": token, "revoked": False})
        # SEC-002 — a fleet deployment_failed alert only fires for a digest
        # the agent's channel actually serves. Grab a real stable digest.
        from release_channels import channel_for_agent
        _ch, shas = await channel_for_agent(db, agent_id)
        return next(iter(shas.values()), None)
    real_sha = _run(seed())
    assert real_sha, "no stable artifact digest to test against"
    try:
        r = requests.post(f"{API}/infra/agent/deploy-status",
                          json={"agent_token": token, "ok": True,
                                "artifact": "stoic-ea", "sha256": real_sha,
                                "detail": "installed"}, timeout=TIMEOUT)
        assert r.status_code == 200 and r.json()["status"] == "success"
        # failure on the ASSIGNED digest → fleet-critical deployment_failed
        r = requests.post(f"{API}/infra/agent/deploy-status",
                          json={"agent_token": token, "ok": False,
                                "artifact": "stoic-ea", "sha256": real_sha,
                                "detail": "sha mismatch"}, timeout=TIMEOUT)
        assert r.status_code == 200 and r.json()["status"] == "failure"
        # failure on an UNASSIGNED digest → anomaly only (no fleet alert)
        bogus = "b" * 64
        r = requests.post(f"{API}/infra/agent/deploy-status",
                          json={"agent_token": token, "ok": False,
                                "artifact": "evil", "sha256": bogus,
                                "detail": "rogue"}, timeout=TIMEOUT)
        assert r.status_code == 200 and r.json()["status"] == "failure"

        async def verify():
            n = await db.agent_deployments.count_documents(
                {"agent_id": agent_id})
            alert = await db.ops_alerts.find_one(
                {"dedup_key": f"deploy_fail:{agent_id}:{real_sha}"})
            anomaly = await db.ops_alerts.find_one(
                {"dedup_key": f"deploy_anom:{agent_id}:{bogus}"})
            return n, alert, anomaly
        n, alert, anomaly = _run(verify())
        assert n == 3
        assert alert and alert["severity"] == "critical"
        assert alert.get("meta", {}).get("agent_id") == agent_id
        assert anomaly and anomaly["severity"] == "warning"
        # bad token rejected
        r = requests.post(f"{API}/infra/agent/deploy-status",
                          json={"agent_token": "agt_tok_bogus", "ok": True},
                          timeout=TIMEOUT)
        assert r.status_code == 401
    finally:
        async def cleanup():
            await db.vps_agents.delete_one({"agent_id": agent_id})
            await db.agent_deployments.delete_many({"agent_id": agent_id})
            await db.ops_alerts.delete_many(
                {"dedup_key": {"$regex": agent_id}})
        _run(cleanup())


def test_host_agent_reports_deploy_status():
    path = os.path.join(_BACKEND_DIR, "..", "docs", "host-agent",
                        "stoic-host-agent.ps1")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "Report-DeployStatus" in src and "deploy-status" in src


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
