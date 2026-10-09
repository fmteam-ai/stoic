"""M117-6 — workers get a heartbeat-file health check (they serve no HTTP, so the image's /api/health HEALTHCHECK
left every worker "unhealthy" forever)."""
import os
import time

import pytest
import yaml

from tests.unit.test_update_preflight_shell import ROOT

pytestmark = pytest.mark.unit
WORKERS = ("trading", "protection", "reconciliation", "analytics", "security", "model", "tuning")


def test_compose_every_worker_uses_the_heartbeat_healthcheck():
    d = yaml.safe_load(open(os.path.join(ROOT, "docker-compose.yml")))
    for w in WORKERS:
        svc = d["services"][f"worker-{w}"]
        hc = svc["healthcheck"]
        assert "heartbeat_healthy" in hc["test"][-1] and hc["start_period"] == "120s" and hc["interval"] == "15s", w
        assert svc["env_file"] == ["backend/.env"] and svc["depends_on"]["mongo"]["condition"] == "service_healthy", w   # app-common kept
        assert svc["command"] == ["python", "-m", f"workers.{w}"]
    assert "healthcheck" not in d["services"]["backend"]        # the API keeps the image's HTTP check
    sec = d["services"]["worker-security"]
    assert sec["environment"]["SECURITY_AGENT_TELEGRAM_BOT_TOKEN_FILE"] == "/run/secrets/security_telegram_token"
    assert sec["environment"]["MONGO_URL_FILE"] == "/run/secrets/mongo_url"   # *app-env merge survives the double anchor


def test_heartbeat_helpers(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://localhost:27017"); monkeypatch.setenv("DB_NAME", "t")
    from workers import base
    p = tmp_path / "hb"
    assert base.heartbeat_healthy(str(p)) is False                       # missing → unhealthy
    monkeypatch.setattr(base, "HEARTBEAT_FILE", str(p))
    base.touch_heartbeat()
    assert base.heartbeat_healthy(str(p)) is True
    os.utime(p, (time.time() - 200, time.time() - 200))
    assert base.heartbeat_healthy(str(p)) is False                       # stale → unhealthy
    assert base.HEARTBEAT_MAX_AGE_SEC > 2 * base.LEASE_RENEW_SEC         # two missed renewals of slack
    src = open(os.path.join(ROOT, "backend", "workers", "base.py")).read()
    assert src.count("touch_heartbeat()") >= 4                            # start, standby loop, acquired, every renew
    assert src.index("lost.set()\n                return\n            touch_heartbeat()") > 0
