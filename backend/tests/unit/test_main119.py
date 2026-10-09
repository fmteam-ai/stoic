"""main119 — M119-1 key separation (refuse, rotate script), M119-2 VirtFS gate + move-docker-root, M119-3 jam resume,
M118-5 lease-based worker health, M119-5 deploy-production wiring, M119-7 release-discipline gate."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from tests.unit.fake_mongo import FakeDb
from tests.unit.test_update_preflight_shell import ROOT, _run, _stub, world  # noqa: F401

pytestmark = pytest.mark.unit


def _sh(script, cwd, env=None):
    return subprocess.run(["bash", "-c", f"set -u; cd '{cwd}'; . {ROOT}/deploy/lib.sh; . {ROOT}/deploy/preflight.sh\n{script}"],
                          capture_output=True, text=True, env=env or os.environ.copy())


# ---------------------------------------------------------------- M119-1
def test_bundle_key_pins_refuse_instead_of_deleting_the_release_pin(tmp_path):
    (tmp_path / "backend").mkdir(); (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "signer_public_key").write_text("SAMEKEY==")
    (tmp_path / "backend" / ".env").write_text("RELEASE_PUBLIC_KEY_B64=SAMEKEY==\nBUNDLE_PUBLIC_KEY_B64=SAMEKEY==\n")
    (tmp_path / ".env").write_text("")
    r = _sh("REF=v9; ensure_bundle_key_pins; echo RC=$?", tmp_path)
    assert "RC=1" in r.stdout and "holds the CI release PRIVATE key" in r.stdout and "deploy/rotate-runtime-key.sh" in r.stdout, r.stdout + r.stderr
    assert "deploy/update.sh v9" in r.stdout
    assert "RELEASE_PUBLIC_KEY_B64=SAMEKEY==" in (tmp_path / "backend" / ".env").read_text()   # pin NOT deleted
    (tmp_path / "secrets" / "signer_public_key").write_text("OTHERKEY==")
    r = _sh("ensure_bundle_key_pins; echo RC=$?", tmp_path)
    assert "RC=0" in r.stdout
    upd = open(os.path.join(ROOT, "deploy", "update.sh")).read()
    assert "ensure_bundle_key_pins || gate_refused" in upd


def test_rotate_runtime_key_script_contract():
    s = open(os.path.join(ROOT, "deploy", "rotate-runtime-key.sh")).read()
    assert "set_kv backend/.env BUNDLE_PUBLIC_KEY_B64" in s and "set_kv backend/.env BUNDLE_SIGNER_KEY_ID" in s and "set_kv .env SIGNER_KEY_ID" in s
    assert "RELEASE_PUBLIC_KEY_B64" not in [l for l in s.splitlines() if "set_kv" in l and "RELEASE" in l]   # never re-pins the release key
    assert "signer_ed25519_key.prev-" in s and "shred -u" in s and "CI release PRIVATE key" in s
    doc = open(os.path.join(ROOT, "deploy", "doctor.sh")).read()
    assert "CI release private key present on this host" in doc and "move-docker-root.sh" in doc
    assert subprocess.run(["bash", "-n", os.path.join(ROOT, "deploy", "rotate-runtime-key.sh")]).returncode == 0


# ---------------------------------------------------------------- M119-2
def test_virtfs_gate_refuses_until_docker_root_leaves_var_lib(world, tmp_path):   # noqa: F811
    vdir = tmp_path / "virtfs"; vdir.mkdir(); (vdir / "user1").mkdir()
    _stub(world["tmp"] / "bin", "docker", '#!/usr/bin/env bash\ncase "$*" in *DockerRootDir*) cat "$STUB_STATE/droot" ;; esac\nexit 0\n')
    os.chmod(world["tmp"] / "bin" / "docker", 0o755)
    (world["state"] / "droot").write_text("/var/lib/docker\n")
    env = {"STOIC_VIRTFS_DIR": str(vdir), "PREFLIGHT_YES": "0", "STOIC_UPDATE_REF": "v1.60.10"}
    r = _run(world, "virtfs_docker_root_gate; echo RC=$?", env)
    assert "RC=1" in r.stdout and "move-docker-root.sh /srv/docker --yes" in r.stdout and "deploy/update.sh v1.60.10" in r.stdout, r.stdout + r.stderr
    (world["state"] / "droot").write_text("/srv/docker\n")
    r = _run(world, "virtfs_docker_root_gate; echo RC=$?", env)
    assert "RC=0" in r.stdout and "outside /var/lib (ok)" in r.stdout
    (vdir / "user1").rmdir()                                   # no jails at all → gate is a no-op
    (world["state"] / "droot").write_text("/var/lib/docker\n")
    r = _run(world, "virtfs_docker_root_gate; echo RC=$?", env)
    assert "RC=0" in r.stdout
    upd = open(os.path.join(ROOT, "deploy", "update.sh")).read()
    assert upd.index("preflight_host || gate_refused") < upd.index("virtfs_docker_root_gate || gate_refused") < upd.index("clean_leftovers || gate_refused")


def test_move_docker_root_refuses_bad_targets():
    s = open(os.path.join(ROOT, "deploy", "move-docker-root.sh")).read()
    for needle in ("rsync -aHAX", '"data-root"', "20-stoic-docker-root.conf", "ROLLBACK", "volumes and images verified identical", "systemctl stop docker"):
        assert needle in s, needle
    assert subprocess.run(["bash", "-n", os.path.join(ROOT, "deploy", "move-docker-root.sh")]).returncode == 0
    r = subprocess.run(["bash", os.path.join(ROOT, "deploy", "move-docker-root.sh"), "/var/lib/docker2"], capture_output=True, text=True)
    assert r.returncode == 1 and ("still under /var/lib" in r.stdout or "run as root" in r.stdout or "docker not installed" in r.stdout)


# ---------------------------------------------------------------- M119-3
def test_update_sh_resume_after_jam_wiring():
    upd = open(os.path.join(ROOT, "deploy", "update.sh")).read()
    assert "JAM_MARKER=deploy/state/update-jam" in upd
    assert 'if [ -f "${JAM_MARKER}" ] && [ -n "${REF}" ] && [ "$(git rev-parse "${REF}^{commit}" 2>/dev/null)" = "${PREV}" ]' in upd
    assert "STOIC_UPDATE_RESUME=1" in upd and 'if [ -n "${STOIC_UPDATE_RESUME:-}" ]; then echo "-- resume' in upd
    assert upd.index('> "${JAM_MARKER}"') < upd.index('rm -f "${JAM_MARKER}"\nclear_deploy_jam_marker')
    pf = open(os.path.join(ROOT, "deploy", "preflight.sh")).read()
    assert "the update RESUMES at the restart step" in pf and "docker ps -aq | xargs -r docker rm -f" not in pf.split("reboot required")[1][:600]


def test_jam_marker_resume_branch_execs_second_stage(tmp_path):
    # exercise the resume condition with a real git repo: HEAD == ref + marker ⇒ exec (we stub the exec target)
    repo = tmp_path / "r"; repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"], cwd=repo, check=True)
    subprocess.run(["git", "tag", "v9"], cwd=repo, check=True)
    (repo / "deploy" / "state").mkdir(parents=True); (repo / "deploy" / "state" / "update-jam").write_text("t jam ref=v9\n")
    upd = open(os.path.join(ROOT, "deploy", "update.sh")).read()
    block = upd[upd.index('  # M119-3 — resume after a jam'):upd.index("  fi\n", upd.index('  # M119-3 — resume after a jam')) + 5]
    script = ("REF=v9; JAM_MARKER=deploy/state/update-jam; PREV=$(git rev-parse HEAD); PREFLIGHT_NO_HOST_CHANGES=0; PREFLIGHT_YES=1\n"
              "readiness_policy() { echo strict; }\nexec() { echo \"EXEC $*\"; return 0; }\n" + block + 'echo "fell through"\n')
    r = subprocess.run(["bash", "-c", script], cwd=repo, capture_output=True, text=True)
    assert "resuming v9 after a jam" in r.stdout and "STOIC_UPDATE_RESUME=1" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- M118-5
def test_worker_health_from_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://localhost:27017"); monkeypatch.setenv("DB_NAME", "t")
    sys.argv = ["x"]
    from workers import base
    ident = tmp_path / "identity"
    db = FakeDb()
    ok, why = asyncio_run(base.health_from_lease(db, str(ident)))
    assert ok is False and "identity not recorded" in why
    monkeypatch.setattr(base, "IDENTITY_FILE", str(ident)); base.record_identity("trading")
    assert ident.read_text().split() == ["trading", base.HOLDER]
    now = datetime.now(timezone.utc)
    asyncio_run(db.worker_leases.insert_one({"_id": "trading", "holder": "someone-else", "expires_at": now + timedelta(seconds=30)}))
    ok, why = asyncio_run(base.health_from_lease(db, str(ident)))
    assert ok is False and "standby" in why
    asyncio_run(db.worker_leases.update_many({"_id": "trading"}, {"$set": {"holder": base.HOLDER}}))
    ok, why = asyncio_run(base.health_from_lease(db, str(ident)))
    assert ok is True
    asyncio_run(db.worker_leases.update_many({"_id": "trading"}, {"$set": {"expires_at": now - timedelta(seconds=5)}}))
    ok, why = asyncio_run(base.health_from_lease(db, str(ident)))
    assert ok is False and "expired" in why
    d = yaml.safe_load(open(os.path.join(ROOT, "docker-compose.yml")))
    for w in ("trading", "protection", "reconciliation", "analytics", "security", "model", "tuning"):
        assert d["services"][f"worker-{w}"]["healthcheck"]["test"] == ["CMD", "python", "-m", "workers.base", "--health"], w
    lib = open(os.path.join(ROOT, "deploy", "lib.sh")).read(); upd = open(os.path.join(ROOT, "deploy", "update.sh")).read()
    assert "wait_workers_healthy()" in lib and "wait_workers_healthy 20 || rollback" in upd
    doc = open(os.path.join(ROOT, "deploy", "doctor.sh")).read()
    assert doc.index("*unhealthy*) fail") < doc.index("*running*healthy*|running) ok")


def asyncio_run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------- M119-5 / M119-7
def test_deploy_production_and_release_discipline_wiring():
    wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "deploy-production.yml")))
    steps = {s.get("name", ""): s for s in wf["jobs"][list(wf["jobs"])[0]]["steps"]}
    pub = next(v for k, v in steps.items() if k.startswith("Publish on server"))
    assert "STOIC_READINESS_POLICY=" in pub["run"] and "PREFLIGHT_YES=1" in pub["run"] and "update-jammed-ebusy" in pub["run"]
    assert "${{ secrets" not in pub["run"]                      # untrusted/secret context only via env
    rep = next(v for k, v in steps.items() if k.startswith("Report"))
    assert "GITHUB_STEP_SUMMARY" in rep["run"] and rep["if"].startswith("always()")
    rel = open(os.path.join(ROOT, ".github", "workflows", "release.yml")).read()
    assert "scripts/check_open_findings.py" in rel and rel.index("check_open_findings.py") < rel.index("ci-gates:")
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import check_open_findings as cof
    b, w = cof.evaluate([{"id": "a", "severity": "P0", "title": "x"}, {"id": "b", "severity": "P1", "title": "y", "tag_waiver": "demo only"},
                         {"id": "c", "severity": "P2", "title": "z"}, {"id": "d", "severity": "P1", "title": "closed", "status": "closed"}])
    assert [f["id"] for f in b] == ["a"] and [f["id"] for f in w] == ["b"]
    assert cof.main() == 0                                      # the committed findings file carries written waivers
    docs = open(os.path.join(ROOT, "docs", "DEPLOYMENT.md")).read()
    for k in ("DEPLOY_HOST", "DEPLOY_USER", "DEPLOY_SSH_KEY", "DEPLOY_PATH", "DEPLOY_PORT", "Release discipline"):
        assert k in docs, k
