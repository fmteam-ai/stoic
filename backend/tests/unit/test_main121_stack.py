"""main121 — M121-5 no mixed stack: every service built from Dockerfile.backend/.frontend is pinned in
docker-compose.registry.yml; verify_stack_images fails a deploy whose containers run different images; readiness
rc_lock / deploy_source fail on a mixed stack instead of passing."""
import json
import os
import shutil
import stat
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
BE_ID = "sha256:" + "b" * 64
FE_ID = "sha256:" + "f" * 64
OLD_ID = "sha256:" + "0" * 64
BE_DIGEST = "ghcr.io/x/stoic-backend@sha256:" + "1" * 64
FE_DIGEST = "ghcr.io/x/stoic-frontend@sha256:" + "2" * 64


def _built_services():
    cfg = yaml.safe_load(open(os.path.join(ROOT, "docker-compose.yml")))
    out = {}
    for name, svc in cfg["services"].items():
        df = ((svc.get("build") or {}).get("dockerfile") or "")
        if df.endswith("Dockerfile.backend"):
            out[name] = "backend"
        elif df.endswith("Dockerfile.frontend"):
            out[name] = "frontend"
    return out


def test_every_built_service_is_pinned_in_the_registry_overlay():
    built = _built_services()
    assert {"backend", "worker-trading", "worker-security", "frontend"} <= set(built)
    reg = yaml.safe_load(open(os.path.join(ROOT, "docker-compose.registry.yml")))["services"]
    missing = [s for s in built if s not in reg or not (reg[s].get("image") or "").startswith("${STOIC_")]
    assert not missing, f"services built from Dockerfile.backend/.frontend but not pinned in docker-compose.registry.yml: {missing}"
    for s, kind in built.items():
        want = "STOIC_BACKEND_IMAGE" if kind == "backend" else "STOIC_FRONTEND_IMAGE"
        assert want in reg[s]["image"] and reg[s].get("pull_policy") == "never", s
    extra = [s for s in reg if s not in built]
    assert not extra, f"registry overlay pins services that are not built from a STOIC Dockerfile: {extra}"


DOCKER_STUB = r'''#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
case "$*" in
  "compose config --format json") cat "$STUB_CFG" ;;
  "compose ps -q "*) svc="${@: -1}"; [ "$svc" = worker-model ] && exit 0; echo "cid-$svc" ;;
  "inspect --format {{.Image}} cid-"*) svc="${@: -1}"; svc="${svc#cid-}"
      if [ "$svc" = frontend ]; then echo "'"$FE_ID"'"; elif [ "$svc" = "${STUB_STALE:-none}" ]; then echo "'"$OLD_ID"'"; else echo "'"$BE_ID"'"; fi ;;
  "inspect --format {{.Id}} "*) img="${@: -1}"; case "$img" in *frontend*) echo "'"$FE_ID"'" ;; *) echo "'"$BE_ID"'" ;; esac ;;
  "inspect --format {{if .RepoDigests}}{{index .RepoDigests 0}}{{end}} "*) img="${@: -1}"
      case "$img" in "'"$FE_ID"'") echo "'"$FE_DIGEST"'" ;; "'"$BE_ID"'") echo "'"$BE_DIGEST"'" ;; *) echo "" ;; esac ;;
  *) exit 0 ;;
esac
'''.replace("'\"$FE_ID\"'", FE_ID).replace("'\"$BE_ID\"'", BE_ID).replace("'\"$OLD_ID\"'", OLD_ID) \
   .replace("'\"$FE_DIGEST\"'", FE_DIGEST).replace("'\"$BE_DIGEST\"'", BE_DIGEST)


def _world(tmp_path):
    root = tmp_path / "root"
    shutil.copytree(os.path.join(ROOT, "deploy"), root / "deploy", ignore=shutil.ignore_patterns("state", "releases", "*.log"))
    (root / "deploy" / "state").mkdir(); (root / "backend").mkdir(); (root / ".env").write_text("APP_ENV=production\n")
    (root / "backend" / "BUILD_SHA").write_text("a" * 40 + "\n")
    cfg = {"services": {s: {"build": {"dockerfile": f"Dockerfile.{k}"}} for s, k in _built_services().items()}}
    cfg["services"]["mongo"] = {"image": "mongo:7"}
    (tmp_path / "cfg.json").write_text(json.dumps(cfg))
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    p = bin_dir / "docker"; p.write_text(DOCKER_STUB); os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    (root / "deploy" / "state" / "deploy_source.json").write_text(json.dumps({"source": "registry", "reason": "attested release"}))
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", STUB_LOG=str(tmp_path / "docker.log"), STUB_CFG=str(tmp_path / "cfg.json"),
               STOIC_BACKEND_IMAGE=BE_DIGEST, STOIC_FRONTEND_IMAGE=FE_DIGEST)
    return root, env


def _run(root, env, script):
    return subprocess.run(["bash", "-c", f"set -uo pipefail; cd '{root}'; . deploy/lib.sh\n{script}"], capture_output=True, text=True, env=env, timeout=60)


def test_verify_stack_images_passes_uniform_stack_and_records_containers(tmp_path):
    root, env = _world(tmp_path)
    r = _run(root, env, "app_services; verify_stack_images; echo RC=$?")
    assert "worker-security backend" in r.stdout and "frontend frontend" in r.stdout and "mongo" not in r.stdout.split("RC=")[0].replace("mongo:7", "")
    assert "RC=0" in r.stdout and "stack images verified" in r.stdout, r.stdout + r.stderr
    d = json.loads((root / "deploy" / "state" / "deploy_source.json").read_text())
    assert d["mixed"] is False and d["source"] == "registry"
    assert d["containers"]["worker-security"] == {"kind": "backend", "image_id": BE_ID, "repo_digest": BE_DIGEST, "status": "ok"}
    assert d["containers"]["frontend"]["repo_digest"] == FE_DIGEST and "worker-model" not in d["containers"]   # not running → skipped


def test_verify_stack_images_fails_mixed_stack(tmp_path):
    root, env = _world(tmp_path)
    r = _run(root, dict(env, STUB_STALE="worker-security"), "verify_stack_images; echo RC=$?")
    assert "RC=1" in r.stdout and "MIXED STACK" in r.stdout, r.stdout + r.stderr
    assert f"!! worker-security runs image {OLD_ID} (local build) — expected {BE_ID}" in r.stdout
    d = json.loads((root / "deploy" / "state" / "deploy_source.json").read_text())
    assert d["mixed"] is True and d["containers"]["worker-security"]["status"] == "MISMATCH" and d["containers"]["backend"]["status"] == "ok"
    up = open(os.path.join(ROOT, "deploy", "update.sh")).read(); rb = open(os.path.join(ROOT, "deploy", "rollback.sh")).read()
    assert "verify_stack_images || rollback" in up and "verify_stack_images ||" in rb


def test_readiness_fails_on_mixed_stack(tmp_path, monkeypatch):
    import release_truth as rt
    state = tmp_path / "state"; (state / "release").mkdir(parents=True)
    monkeypatch.setattr(rt, "STATE_DIR", str(state))
    monkeypatch.setattr(rt, "LOCK_PATHS", (str(state / "release" / "rc_lock.json"),))
    (state / "release" / "rc_lock.json").write_text(json.dumps({"authoritative": True, "git_commit": "a" * 40,
                                                                  "images": {"backend": BE_DIGEST, "frontend": FE_DIGEST}}))
    monkeypatch.setenv("STOIC_IMAGE_DIGEST", BE_DIGEST.split("@")[1])
    import modules.pamm.strategy_guard as sg
    monkeypatch.setattr(sg, "GIT_COMMIT", "a" * 40)
    good = {"backend": {"kind": "backend", "repo_digest": BE_DIGEST, "status": "ok"},
            "frontend": {"kind": "frontend", "repo_digest": FE_DIGEST, "status": "ok"}}
    (state / "deploy_source.json").write_text(json.dumps({"source": "registry", "containers": good, "mixed": False}))
    assert rt.rc_lock_check(True)["ok"] and rt.rc_lock_check(True)["all_containers_match"] is True
    assert rt.deploy_source_check()["ok"] and rt.deploy_source_check()["mixed"] is False
    bad = dict(good, **{"worker-security": {"kind": "backend", "repo_digest": None, "status": "MISMATCH"}})
    (state / "deploy_source.json").write_text(json.dumps({"source": "registry", "containers": bad, "mixed": True}))
    c = rt.rc_lock_check(True)
    assert c["ok"] is False and c["containers_stale"] == ["worker-security"] and "mixed stack" in c["note"]
    d = rt.deploy_source_check()
    assert d["ok"] is False and d["severity"] == "block" and "worker-security" in d["detail"] and "--reprovision" in d["fix"]
    # a backend-kind container running a digest that is not the locked one is stale even if it has a repo digest
    bad2 = dict(good, **{"worker-tuning": {"kind": "backend", "repo_digest": "ghcr.io/x/stoic-backend@sha256:" + "9" * 64, "status": "ok"}})
    (state / "deploy_source.json").write_text(json.dumps({"source": "registry", "containers": bad2, "mixed": False}))
    assert rt.rc_lock_check(True)["containers_stale"] == ["worker-tuning"]
    assert rt.rc_lock_check(False)["ok"] is True                                   # never enforced outside production
