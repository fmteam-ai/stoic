"""Unit — deploy/migrator/migrator.py state machine (commands mocked) +
sidecar HTTP auth + API proxy routes against a fake sidecar."""
import importlib.util
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SCRIPT = os.path.join(ROOT, "deploy", "migrator", "migrator.py")


def _mod():
    spec = importlib.util.spec_from_file_location("stoic_migrator", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PREFLIGHT_OK = """OS=AlmaLinux 8.10
ARCH=x86_64
DOCKER=Docker version 27.1.1
COMPOSE=Docker Compose version v2.29.1
RSYNC=ok
SUDO=ok
DOCKER_ACCESS=ok
PATH_ENTRIES=0
DISK_AVAIL=500000000000
MEM_TOTAL=68719476736
PORTS_BUSY=
PUBLIC_IP=203.0.113.10
"""


class FakeRunner:
    """Answers commands by substring match; records everything."""
    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, cmd, step, timeout):
        self.calls.append((step, cmd))
        for key, (rc, out) in self.answers.items():
            if key in cmd:
                return rc, out() if callable(out) else out
        if "ssh-keyscan" in cmd:
            return 0, "203.0.113.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample\n"
        if "ssh-keygen -lf" in cmd:
            return 0, "256 SHA256:abcFINGERPRINT 203.0.113.10 (ED25519)\n"
        if "ssh-keygen -q" in cmd:
            os.makedirs(os.path.dirname(self.pub), exist_ok=True)
            open(self.pub, "w").write("ssh-ed25519 AAAA stoic-migrator\n")
            open(self.pub[:-4], "w").write("PRIVATE\n")
            return 0, ""
        if "git rev-parse" in cmd:
            return 0, "a" * 40 + "\n"
        if "git tag --points-at" in cmd:
            return 0, "v1.4.2\n"
        if "du -sb /data/db" in cmd:
            return 0, "2000000000\n"
        if "du -sb backups" in cmd:
            return 0, "300000000\n"
        if "countDocuments" in cmd:
            return 0, "3\n"
        if "bash -s" in cmd and "PATH_ENTRIES" in cmd:
            return 0, PREFLIGHT_OK
        if "READY=" in cmd:
            return 0, 'READY={"ready": true, "workers": 6}\n'
        if "curl -fsS http://127.0.0.1:8001/api/health" in cmd:
            return 0, '{"status":"ok","build_sha":"' + "a" * 40 + '"}'
        return 0, ""


@pytest.fixture
def mig(tmp_path, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "ROOT", str(tmp_path))
    monkeypatch.setattr(m, "STATE_DIR", str(tmp_path / "release/migration"))
    monkeypatch.setattr(m, "KEY_FILE", str(tmp_path / "release/migration/id_ed25519"))
    monkeypatch.setattr(m, "KNOWN_HOSTS", str(tmp_path / "release/migration/known_hosts"))
    (tmp_path / ".env").write_text("COMPOSE_FILE=docker-compose.yml:docker-compose.tls.yml\nDOMAIN=trade.example.com\nDEPLOY_MODE=registry\n")
    runner = FakeRunner()
    runner.pub = str(tmp_path / "release/migration/id_ed25519.pub")
    inst = m.Migrator(run=runner, state_file=str(tmp_path / "release/migration/state.json"))
    inst._runner, inst._m = runner, m
    return inst


TARGET = {"host": "203.0.113.10", "user": "stoic", "port": 22, "path": "/home/stoic/stoic"}


def _wait(mig, timeout=5):
    t0 = time.time()
    while mig.state["status"] == "running" and time.time() - t0 < timeout:
        time.sleep(0.02)
    if mig.thread:
        mig.thread.join(timeout=1)


def test_public_key_generated_once(mig):
    k1 = mig.public_key()
    k2 = mig.public_key()
    assert k1.startswith("ssh-ed25519") and k1 == k2
    assert sum("ssh-keygen -q" in c for _, c in mig._runner.calls) == 1


def test_preflight_ok_collects_facts_and_awaits_install(mig):
    facts = mig.preflight(TARGET)
    assert facts["ok"] is True
    assert all(facts["target"]["checks"].values())
    assert facts["source"] == {"sha": "a" * 40, "tag": "v1.4.2", "compose_file": "docker-compose.yml:docker-compose.tls.yml",
                               "tls": True, "forecast": False, "registry": True, "domain": "trade.example.com",
                               "project": os.path.basename(mig._m.ROOT).lower(), "db_bytes": 2000000000,
                               "backups_bytes": 300000000, "connected_accounts": 3}
    assert facts["host_key"]["fingerprints"] == ["SHA256:abcFINGERPRINT"]
    assert mig.state["status"] == "awaiting" and mig.state["awaiting"] == "install"
    assert mig.state["target"]["public_ip"] == "203.0.113.10"
    # host key was pinned for every later ssh call
    assert os.path.exists(mig._m.KNOWN_HOSTS)
    assert "StrictHostKeyChecking=yes" in mig.ssh_prefix()


def test_preflight_fails_on_busy_ports_and_small_disk(mig):
    bad = PREFLIGHT_OK.replace("PORTS_BUSY=", "PORTS_BUSY=80,443").replace("DISK_AVAIL=500000000000", "DISK_AVAIL=1000")
    mig._runner.answers["PATH_ENTRIES"] = (0, bad)
    facts = mig.preflight(TARGET)
    assert facts["ok"] is False
    assert facts["target"]["checks"]["ports_free"] is False and facts["target"]["checks"]["disk"] is False
    assert mig.state["status"] == "failed" and "ports_free" in mig.state["error"]
    with pytest.raises(RuntimeError):
        mig.start()


def test_fingerprint_mismatch_rejected(mig):
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        mig.preflight(TARGET, accept_fingerprint="SHA256:other")
    assert mig.state["status"] == "failed"


def test_full_happy_path_with_gates(mig):
    mig.preflight(TARGET)
    mig.start()
    _wait(mig)
    assert mig.state["status"] == "awaiting" and mig.state["awaiting"] == "freeze", mig.state["error"]
    cmds = [c for _, c in mig._runner.calls]
    joined = "\n".join(cmds)
    # install: rsync excludes the migrator's own key material, carries TLS certs, right install flags, workers stopped
    assert "rsync -az --delete" in joined and "--exclude=release/migration" in joined
    assert "_caddy_data" in joined and "tar cz -C /data" in joined
    assert "deploy/install.sh --production trade.example.com --registry" in joined
    assert "docker compose stop worker-trading" in joined
    # warm sync = dump piped into remote restore --drop, source NOT stopped
    assert "mongodump" in joined and "mongorestore" in joined and "--drop" in joined
    assert not any(c.startswith("docker compose stop backend") for c in cmds)
    assert mig.state["downtime_started_at"] is None

    with pytest.raises(RuntimeError):          # gates are strict
        mig.advance("decommission")
    mig.advance("freeze")
    _wait(mig)
    assert mig.state["status"] == "awaiting" and mig.state["awaiting"] == "cutover_check", mig.state["error"]
    assert mig.state["downtime_started_at"] is not None
    after = [c for _, c in mig._runner.calls[len(cmds):]]
    assert any(c.startswith("docker compose stop backend worker-trading") for c in after)
    assert sum("mongorestore" in c for c in after) == 1
    assert any("docker compose up -d" in c for c in after)        # target workers start only now
    assert mig._step("verify_target")["detail"]["readiness"] == {"ready": True, "workers": 6}

    # cutover: heartbeats not yet on target → stays awaiting cutover_check
    mig._runner.answers["countDocuments({last_heartbeat"] = (0, "0\n")
    mig._runner.answers["curl -fsS -m 10"] = (0, '{"status":"ok"}')
    mig.advance("cutover_check")
    _wait(mig)
    assert mig.state["awaiting"] == "cutover_check"
    assert mig.state["facts"]["cutover"]["ok"] is False
    with pytest.raises(RuntimeError, match="cutover not confirmed"):
        mig.advance("decommission")
    # EAs arrive → gate opens
    mig._runner.answers["countDocuments({last_heartbeat"] = (0, "2\n")
    mig.advance("cutover_check")
    _wait(mig)
    assert mig.state["awaiting"] == "decommission"
    assert mig.state["facts"]["cutover"]["ea_heartbeats_on_target"] == 2
    mig.advance("decommission")
    _wait(mig)
    assert mig.state["status"] == "done"
    assert os.path.exists(os.path.join(mig._m.STATE_DIR, "DECOMMISSIONED"))
    assert [s["status"] for s in mig.state["steps"]] == ["done"] * 8
    with pytest.raises(RuntimeError):
        mig.abort()


def test_failed_step_then_retry_and_abort_restores_source(mig):
    mig.preflight(TARGET)
    mig.start()
    _wait(mig)
    mig._runner.answers["docker compose stop backend"] = (1, "boom")
    mig.advance("freeze")
    _wait(mig)
    assert mig.state["status"] == "failed" and mig._step("freeze")["status"] == "failed"
    del mig._runner.answers["docker compose stop backend"]
    mig.retry()
    _wait(mig)
    assert mig.state["awaiting"] == "cutover_check", mig.state["error"]
    res = mig.abort()
    assert res["restarted_source"] is True
    cmds = [c for _, c in mig._runner.calls]
    assert any(c == "docker compose up -d" for c in cmds)          # source stack back
    assert mig.state["status"] == "aborted"
    # target workers stopped again so two hosts never trade at once
    assert any("docker compose stop worker-trading" in c for c in cmds[-4:])


def test_abort_before_freeze_does_not_touch_source(mig):
    mig.preflight(TARGET)
    mig.start()
    _wait(mig)
    n = len(mig._runner.calls)
    res = mig.abort()
    assert res["restarted_source"] is False
    assert not any(c == "docker compose up -d" for _, c in mig._runner.calls[n:])


def test_state_persists_and_reloads(mig):
    mig.preflight(TARGET)
    again = mig._m.Migrator(run=mig._runner, state_file=mig.state_file)
    assert again.state["id"] == mig.state["id"] and again.state["awaiting"] == "install"


# ── sidecar HTTP auth ────────────────────────────────────────────────────────
def _serve(mig, token):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mig._m.make_handler(mig, token))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_http_requires_token_and_reports_conflicts(mig):
    import urllib.request
    import urllib.error
    srv = _serve(mig, "s3cret")
    base = f"http://127.0.0.1:{srv.server_port}"
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/status")
    assert e.value.code == 401
    req = urllib.request.Request(base + "/status", headers={"X-Migrator-Token": "s3cret"})
    assert json.load(urllib.request.urlopen(req))["status"] == "idle"
    req = urllib.request.Request(base + "/start", data=b"{}", method="POST",
                                 headers={"X-Migrator-Token": "s3cret"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 409 and "preflight" in json.load(e.value)["error"]
    srv.shutdown()


# ── API proxy routes (backend) ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_api_proxy_status_and_not_enabled(mig, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from auth import get_current_user
    from routes import host_migration_routes as hm

    app = FastAPI()
    app.include_router(hm.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "role": "admin", "email": "a@x", "two_factor_enabled": True}
    monkeypatch.setenv("ADMIN_MFA_ENFORCED", "false")
    monkeypatch.delenv("MIGRATOR_URL", raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/admin/host-migration/status")
        assert r.status_code == 503 and r.json()["detail"]["code"] == "migrator_not_enabled"

        srv = _serve(mig, "tok123")
        monkeypatch.setenv("MIGRATOR_URL", f"http://127.0.0.1:{srv.server_port}")
        monkeypatch.setenv("METRICS_TOKEN", "tok123")
        r = await c.get("/admin/host-migration/status")
        assert r.status_code == 200 and r.json()["state"]["status"] == "idle"
        r = await c.get("/admin/host-migration/public-key")
        assert r.status_code == 200 and r.json()["public_key"].startswith("ssh-ed25519")
        r = await c.post("/admin/host-migration/preflight", json={"host": "bad host!", "user": "stoic", "path": "/x"})
        assert r.status_code == 422
        r = await c.post("/admin/host-migration/advance", json={"step": "nuke"})
        assert r.status_code == 422
        monkeypatch.setenv("METRICS_TOKEN", "wrong")
        r = await c.get("/admin/host-migration/status")
        assert r.status_code == 502
        srv.shutdown()

    app.dependency_overrides[get_current_user] = lambda: {"id": "u2", "role": "user", "email": "b@x"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/admin/host-migration/status")).status_code == 403


def test_compose_overlay_and_dockerfile_wiring():
    import yaml
    reg = yaml.safe_load(open(os.path.join(ROOT, "docker-compose.migrator.yml")))
    mg = reg["services"]["migrator"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in mg["volumes"]
    assert "ports" not in mg, "sidecar must never be exposed on a host port"
    assert reg["services"]["backend"]["environment"]["MIGRATOR_URL"] == "http://migrator:8790"
    df = open(os.path.join(ROOT, "Dockerfile.migrator")).read()
    assert "openssh-client" in df and "rsync" in df
    mk = open(os.path.join(ROOT, "Makefile")).read()
    assert "migrator-on:" in mk and "migrator-off:" in mk
