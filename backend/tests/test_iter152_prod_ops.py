"""iter-152 · Production ops round: Prometheus metrics endpoint, request IDs
+ structured access logs, execution-health infra extras (clock skew, feeds,
ws clients), certification round 2 (stop/freeze levels + demo certification
stamp), documented .env.example, runbooks, extended CI."""
import os as _os
import requests
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)

from dotenv import load_dotenv  # noqa: E402
from live_target import require_live_base_url
load_dotenv(_os.path.join(_BACKEND_DIR, ".env"))

BASE = require_live_base_url()


def _login():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=15)
    assert r.status_code == 200, r.text
    return s


# ------------------------------------------------ Prometheus metrics
def test_metrics_requires_token():
    r = requests.get(f"{BASE}/api/metrics", timeout=15)
    assert r.status_code == 403


def test_metrics_wrong_token_403():
    r = requests.get(f"{BASE}/api/metrics",
                     headers={"X-Metrics-Token": "wrong"}, timeout=15)
    assert r.status_code == 403


def test_metrics_exposition():
    tok = _os.environ["METRICS_TOKEN"]
    r = requests.get(f"{BASE}/api/metrics",
                     headers={"X-Metrics-Token": tok}, timeout=25)
    assert r.status_code == 200, r.text
    body = r.text
    for name in ("stoic_mongo_latency_ms", "stoic_trades",
                 "stoic_unresolved_submissions", "stoic_unprotected_open",
                 "stoic_outbox_pending", "stoic_ws_clients"):
        assert name in body, f"metrics missing {name}"
    assert "# TYPE stoic_mongo_latency_ms gauge" in body
    # bearer form also accepted
    r2 = requests.get(f"{BASE}/api/metrics",
                      headers={"Authorization": f"Bearer {tok}"}, timeout=25)
    assert r2.status_code == 200


# ------------------------------------ request IDs / structured logging
def test_request_id_header_generated():
    r = requests.get(f"{BASE}/api/health", timeout=15)
    assert r.headers.get("X-Request-ID"), "missing X-Request-ID"


def test_request_id_propagated():
    r = requests.get(f"{BASE}/api/health",
                     headers={"X-Request-ID": "trace-abc-123"}, timeout=15)
    assert r.headers.get("X-Request-ID") == "trace-abc-123"


# ------------------------------------------- execution-health infra v2
def test_execution_health_infra_extras():
    s = _login()
    j = s.get(f"{BASE}/api/bot/execution-health", timeout=25).json()
    infra = j["infra"]
    assert "ws_clients" in infra and "feeds" in infra \
        and "feeds_stale" in infra
    for hb in infra["heartbeats"]:
        assert "clock_offset_sec" in hb
    for f in infra["feeds"]:
        assert {"key", "age_sec", "write_ok", "fresh"} <= set(f)


# --------------------------------------------- certification round 2
def test_certification_new_checks():
    s = _login()
    items = s.get(f"{BASE}/api/accounts/certification", timeout=20).json()["items"]
    keys = {c["key"] for c in items[0]["checks"]}
    assert "stop_freeze_levels" in keys
    assert "demo_certified" in keys
    assert "can_certify" in items[0]


def test_certify_endpoint_blocks_when_checks_fail():
    s = _login()
    items = s.get(f"{BASE}/api/accounts/certification", timeout=20).json()["items"]
    blocked = next((i for i in items if not i["can_certify"]), None)
    if blocked is None:
        import pytest
        pytest.skip("all accounts currently pass base checks")
    r = s.post(f"{BASE}/api/accounts/{blocked['account_id']}/certify",
               timeout=20)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "certification_blocked"
    assert r.json()["detail"]["failing"]


# ----------------------------------------------- artifacts + docs
def test_env_example_documents_all_keys():
    example = open(_os.path.join(_BACKEND_DIR, ".env.example")).read()
    real_keys = [l.split("=", 1)[0] for l in
                 open(_os.path.join(_BACKEND_DIR, ".env")) if "=" in l]
    for k in real_keys:
        assert f"{k}=" in example, f".env.example missing {k}"
    # no secrets leaked — every line is either comment or KEY=
    for line in example.splitlines():
        if line and not line.startswith("#"):
            assert line.endswith("="), f"value leaked in .env.example: {line}"
    fe = open(_os.path.join(_REPO_DIR, "frontend", ".env.example")).read()
    assert "REACT_APP_BACKEND_URL=" in fe


def test_runbooks_exist():
    for doc in ("RUNBOOK.md", "DISASTER_RECOVERY.md", "ROLLBACK.md",
                "INCIDENT_RESPONSE.md"):
        p = _os.path.join(_REPO_DIR, "docs", doc)
        assert _os.path.exists(p), f"missing docs/{doc}"
        assert len(open(p).read()) > 500, f"docs/{doc} is a stub"


def test_ci_has_extended_jobs():
    ci = open(_os.path.join(_REPO_DIR, ".github", "workflows", "ci.yml")).read()
    for job in ("backend-unit", "ea-structural-check", "frontend-build",
                "security-scan", "static-analysis", "container-build"):
        assert f"  {job}:" in ci, f"ci.yml missing job {job}"
    assert "sbom-action" in ci


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
