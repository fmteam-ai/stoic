"""main120 — A18 Part 3 registry deploy default: DEPLOY_MODE auto (registry when the ref is an attested release,
on-host build otherwise / on pull failure), strict `registry`, `build`; attestation adopted opportunistically in
auto mode even with ATTESTATION_REQUIRED=false; host truth published to deploy/state/release for the backend;
update.sh reads STOIC_READINESS_POLICY / UPDATE_HOLD_ON_FAILURE from ./.env; rollback wiring; doctor key ages."""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.test_release_attestation import _emit, SHA  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
BE = "ghcr.io/x/stoic-backend@sha256:" + "1" * 64
FE = "ghcr.io/x/stoic-frontend@sha256:" + "2" * 64

DOCKER_STUB = r'''#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
case "$*" in
  "compose build"*) exit "${STUB_BUILD_RC:-0}" ;;
  "compose config --images"*) echo stoic-backend; echo stoic-frontend ;;
  "inspect --format {{.Id}} "*) echo "sha256:$(printf 'b%.0s' $(seq 1 64))" ;;
  "pull -q "*) exit "${STUB_PULL_RC:-0}" ;;
  "inspect --format {{index .RepoDigests 0}} "*) echo "${@: -1}" ;;
  "login "*) exit 0 ;;
  *) exit 0 ;;
esac
'''
GIT_STUB = r'''#!/usr/bin/env bash
case "$1 $2" in
  "rev-parse --short") echo "${3:-HEAD}" | cut -c1-8 ;;
  "rev-parse HEAD") cat backend/BUILD_SHA ;;
  "tag --points-at") echo v9.9.9 ;;
  "remote get-url") echo https://github.com/x/y.git ;;
  *) exit 0 ;;
esac
'''


def _stub(bin_dir, name, body):
    p = bin_dir / name; p.write_text(body); os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)


def _world(tmp_path, env_lines=("APP_ENV=production", "GITHUB_REPO=x/y"), attested=True, fetch_ok=False):
    root = tmp_path / "root"
    shutil.copytree(os.path.join(ROOT, "deploy"), root / "deploy", ignore=shutil.ignore_patterns("state", "releases", "*.log"))
    (root / "scripts").mkdir()
    for s in ("release_attestation.py", "verify_admission.py", "release_consistency_check.py"):
        shutil.copy(os.path.join(ROOT, "scripts", s), root / "scripts" / s)
    (root / "release").mkdir(); (root / "backend").mkdir(); (root / "docs").mkdir(); (root / "deploy" / "state").mkdir()
    (root / "backend" / "BUILD_SHA").write_text(SHA + "\n")
    (root / ".env").write_text("\n".join(env_lines) + "\n")
    (root / "backend" / ".env").write_text("APP_ENV=production\n")
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _stub(bin_dir, "docker", DOCKER_STUB); _stub(bin_dir, "git", GIT_STUB)
    _stub(bin_dir, "cosign", "#!/usr/bin/env bash\nexit 0\n")
    # signed release assets as release.yml publishes them (attestation + admission pair + rc_lock + SHA256SUMS)
    assets = tmp_path / "assets"; assets.mkdir()
    _, att = _emit(tmp_path)
    shutil.copy(att, assets / "release-attestation.json")
    for ext in (".sig", ".pem"):
        (assets / f"release-attestation.json{ext}").write_text("x")
    subprocess.run([sys.executable, str(root / "scripts" / "verify_admission.py"), "--emit", str(assets / "release-admission.json"),
                    "--backend-digest", BE, "--frontend-digest", FE, "--tag", "v9.9.9", "--commit", SHA], check=True, capture_output=True)
    for ext in (".sig", ".pem"):
        (assets / f"release-admission.json{ext}").write_text("x")
    shutil.copy(tmp_path / "rc_lock.json", assets / "rc_lock.json")
    (assets / "RELEASE_SUMMARY.md").write_text("# frozen summary\n")
    sums = "".join(f"{hashlib.sha256((assets / n).read_bytes()).hexdigest()}  {n}\n" for n in ("rc_lock.json", "RELEASE_SUMMARY.md"))
    (assets / "SHA256SUMS").write_text(sums)
    for ext in (".sig", ".pem"):
        (assets / f"SHA256SUMS{ext}").write_text("x")
    # `release_attestation.py fetch` never touches the network here: copy the assets (fetch_ok) or fail like an outage
    real = root / "scripts" / "release_attestation.py"; real.rename(root / "scripts" / "_release_attestation_real.py")
    real.write_text(f'''#!/usr/bin/env python3
import os, shutil, subprocess, sys
if len(sys.argv) > 1 and sys.argv[1] == "fetch":
    if os.environ.get("STUB_FETCH_OK") != "1":
        print("fetch failed: https://api.github.com/... <urlopen error>", file=sys.stderr); sys.exit(3)
    dest = sys.argv[sys.argv.index("--dest") + 1]; os.makedirs(dest, exist_ok=True)
    for f in os.listdir("{assets}"): shutil.copy(os.path.join("{assets}", f), dest)
    sys.exit(0)
sys.exit(subprocess.call([sys.executable, os.path.join(os.path.dirname(__file__), "_release_attestation_real.py"), *sys.argv[1:]]))
''')
    if attested:   # what verify_attestation leaves behind for THIS commit
        shutil.copy(assets / "release-attestation.json", root / "release" / "attestation.current.json")
        (root / "release" / "attestation").mkdir()
        for f in os.listdir(assets):
            shutil.copy(assets / f, root / "release" / "attestation" / f)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", STUB_LOG=str(tmp_path / "docker.log"),
               STUB_FETCH_OK="1" if fetch_ok else "0", PREFLIGHT_YES="1")
    return root, env


def _run(root, env, script):
    return subprocess.run(["bash", "-c", f"set -uo pipefail; cd '{root}'; . deploy/lib.sh\n{script}"], capture_output=True, text=True, env=env, timeout=120)


def _log(env):
    return open(env["STUB_LOG"]).read() if os.path.exists(env["STUB_LOG"]) else ""


def _src(root):
    return json.loads((root / "deploy" / "state" / "deploy_source.json").read_text())


def test_auto_mode_pulls_attested_digests_without_flipping_deploy_mode(tmp_path):
    root, env = _world(tmp_path)
    r = _run(root, env, "deploy_mode; provision_images; echo RC=$?")
    assert r.stdout.startswith("auto\n") and "RC=0" in r.stdout, r.stdout + r.stderr
    log = _log(env)
    assert f"docker pull -q {BE}" in log and f"docker pull -q {FE}" in log and "compose build" not in log
    envf = (root / ".env").read_text()
    assert f"STOIC_BACKEND_IMAGE={BE}" in envf and "docker-compose.registry.yml" in envf and "STOIC_IMAGE_DIGEST=sha256:" + "1" * 64 in envf
    assert "DEPLOY_MODE=" not in envf                                        # auto stays auto (fallback remains possible)
    assert _src(root)["source"] == "registry" and "attested release" in _src(root)["reason"]
    assert "admission gate: PASSED" in r.stdout and "image provenance (registry)" in r.stdout


def test_auto_mode_falls_back_to_on_host_build_when_pull_fails(tmp_path):
    root, env = _world(tmp_path)
    (root / ".env").write_text("APP_ENV=production\nGITHUB_REPO=x/y\nCOMPOSE_FILE=docker-compose.yml:docker-compose.tls.yml:docker-compose.registry.yml\n"
                               f"STOIC_BACKEND_IMAGE={BE}\nSTOIC_FRONTEND_IMAGE={FE}\n")   # a previous registry deploy
    r = _run(root, dict(env, STUB_PULL_RC="1"), "provision_images; echo RC=$?")
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    assert "falling back to an on-host build" in r.stdout and "GHCR outage" in r.stdout
    log = _log(env)
    assert "docker pull -q" in log and "docker compose build" in log
    envf = (root / ".env").read_text()
    assert "COMPOSE_FILE=docker-compose.yml:docker-compose.tls.yml\n" in envf        # registry overlay dropped
    assert "STOIC_BACKEND_IMAGE" not in envf and "STOIC_FRONTEND_IMAGE" not in envf
    assert "STOIC_IMAGE_DIGEST=sha256:" + "b" * 64 in envf                           # the locally built image id
    s = _src(root)
    assert s["source"] == "build-fallback" and "pull/verification failed" in s["reason"]
    assert "registry-fallback-build" in (root / "deploy" / "releases.log").read_text()


def test_strict_registry_mode_never_builds(tmp_path):
    root, env = _world(tmp_path, env_lines=("APP_ENV=production", "GITHUB_REPO=x/y", "DEPLOY_MODE=registry"))
    r = _run(root, dict(env, STUB_PULL_RC="1"), "provision_images; echo RC=$?")
    assert "RC=1" in r.stdout and "compose build" not in _log(env), r.stdout + r.stderr
    assert not (root / "deploy" / "state" / "deploy_source.json").exists()
    r = _run(root, env, "provision_images; echo RC=$?")                                # pull works → registry
    assert "RC=0" in r.stdout and _src(root)["source"] == "registry" and _src(root)["reason"] == "DEPLOY_MODE=registry"


def test_auto_without_attestation_and_explicit_build_mode(tmp_path):
    root, env = _world(tmp_path, attested=False)
    r = _run(root, env, "registry_available; echo RA=$?; provision_images; echo RC=$?")
    assert "RA=1" in r.stdout and "RC=0" in r.stdout and "no attested images" in r.stdout, r.stdout + r.stderr
    assert "docker compose build" in _log(env) and "pull -q" not in _log(env)
    assert _src(root) == {**_src(root), "source": "build", "reason": "no attested images for this ref", "deploy_mode": "auto"}
    root2, env2 = _world(tmp_path / "b", env_lines=("APP_ENV=production", "GITHUB_REPO=x/y", "DEPLOY_MODE=build",
                                                  "COMPOSE_FILE=docker-compose.yml:docker-compose.registry.yml"))
    r = _run(root2, env2, "provision_images; echo RC=$?")
    assert "RC=0" in r.stdout and "pull -q" not in _log(env2)
    assert "COMPOSE_FILE=docker-compose.yml\n" in (root2 / ".env").read_text() and _src(root2)["source"] == "build"


def test_auto_mode_adopts_signed_release_even_with_attestation_not_required(tmp_path):
    # the VPS case: ATTESTATION_REQUIRED=false (demo-only onboarding) kept the developer lock → gates red
    root, env = _world(tmp_path, env_lines=("APP_ENV=production", "GITHUB_REPO=x/y", "ATTESTATION_REQUIRED=false"), attested=False, fetch_ok=True)
    (root / "release" / "rc_lock.json").write_text(json.dumps({"authoritative": False, "git_commit": "dev"}))
    r = _run(root, env, "attestation_required; echo AR=$?; verify_attestation; echo VA=$?; adopt_release_lock; echo AD=$?; provision_images; echo RC=$?")
    assert "AR=1" in r.stdout and "VA=0" in r.stdout and "AD=0" in r.stdout and "RC=0" in r.stdout, r.stdout + r.stderr
    assert "attestation gate: PASSED" in r.stdout and "authoritative rc_lock of v9.9.9 adopted" in r.stdout
    lock = json.loads((root / "release" / "rc_lock.json").read_text())
    assert lock["authoritative"] is True and lock["git_commit"] == SHA
    assert (root / "backend" / "BUILD_SHA").read_text().strip() == SHA
    assert (root / "deploy" / "releases" / f"rc_lock-{SHA}.json").exists()
    # host truth published for the backend (ro mount /app/state)
    pub = root / "deploy" / "state" / "release"
    assert json.loads((pub / "rc_lock.json").read_text())["authoritative"] is True
    assert json.loads((pub / "attestation.current.json").read_text())["commit"] == SHA
    assert (root / "docs" / "RELEASE_SUMMARY.md").read_text() == "# frozen summary\n"
    assert _src(root)["source"] == "registry"
    # same host, release assets unreachable (outage / untagged ref) → no gate, developer lock, on-host build
    root2, env2 = _world(tmp_path / "b", env_lines=("APP_ENV=production", "GITHUB_REPO=x/y", "ATTESTATION_REQUIRED=false"), attested=False, fetch_ok=False)
    (root2 / "release" / "rc_lock.json").write_text(json.dumps({"authoritative": False, "git_commit": "dev"}))
    r = _run(root2, env2, "verify_attestation; echo VA=$?; adopt_release_lock; echo AD=$?; provision_images; echo RC=$?")
    assert "VA=0" in r.stdout and "AD=0" in r.stdout and "RC=0" in r.stdout, r.stdout + r.stderr
    assert "registry path unavailable: GitHub unreachable" in r.stdout and "building on the host instead" in r.stdout and "developer lock kept" in r.stdout
    assert _src(root2)["reason"].startswith("GitHub unreachable")                 # M121-4 — the real reason, recorded
    assert json.loads((root2 / "release" / "rc_lock.json").read_text())["authoritative"] is False
    assert not (root2 / "release" / "attestation.current.json").exists() and _src(root2)["source"] == "build"
    # ATTESTATION_REQUIRED (production default) + outage → gate refuses (unchanged fail-closed behaviour)
    root3, env3 = _world(tmp_path / "c", attested=False, fetch_ok=False)
    r = _run(root3, env3, "verify_attestation; echo VA=$?")
    assert "VA=1" in r.stdout


def test_release_truth_prefers_host_state_and_reports_deploy_source(tmp_path, monkeypatch):
    import release_truth as rt
    state = tmp_path / "state"; (state / "release").mkdir(parents=True)
    monkeypatch.setattr(rt, "STATE_DIR", str(state))
    monkeypatch.setattr(rt, "ATT_PATHS", (str(state / "release" / "attestation.current.json"),) + rt.ATT_PATHS[1:])
    monkeypatch.setattr(rt, "LOCK_PATHS", (str(state / "release" / "rc_lock.json"),) + rt.LOCK_PATHS[1:])
    (state / "release" / "rc_lock.json").write_text(json.dumps({"authoritative": True, "git_commit": SHA, "images": {"backend": BE}}))
    monkeypatch.setenv("STOIC_IMAGE_DIGEST", "sha256:" + "1" * 64)
    import modules.pamm.strategy_guard as sg
    monkeypatch.setattr(sg, "GIT_COMMIT", SHA)
    chk = rt.rc_lock_check(production=True)
    assert chk["ok"] and chk["path"].startswith(str(state)) and chk["digest_matches_running"]
    d = rt.deploy_source_check()
    assert d["available"] is False and d["ok"] and d["severity"] == "ok"
    (state / "deploy_source.json").write_text(json.dumps({"source": "build-fallback", "reason": "registry pull/verification failed", "commit": SHA}))
    d = rt.deploy_source_check()
    assert d["severity"] == "warn" and "REGISTRY FALLBACK" in d["detail"] and d["fix"].startswith("deploy/update.sh")
    (state / "deploy_source.json").write_text(json.dumps({"source": "registry", "reason": "attested release abc"}))
    d = rt.deploy_source_check()
    assert d["severity"] == "ok" and "signed CI images" in d["detail"] and d["fix"] is None
    ops = open(os.path.join(ROOT, "backend", "routes", "ops_routes.py")).read()
    assert 'checks["deploy_source"]' in ops
    assert "/app/state" in open(os.path.join(ROOT, "docker-compose.yml")).read()


def test_update_reads_policy_and_hold_from_dotenv(tmp_path):
    root, env = _world(tmp_path, attested=False)
    (root / ".env").write_text("APP_ENV=production\nSTOIC_READINESS_POLICY=onboarding-close-only\nUPDATE_HOLD_ON_FAILURE=1\n")
    _stub(tmp_path / "bin", "flock", "#!/usr/bin/env bash\nexit 1\n")       # stop right after the policy line
    r = subprocess.run(["bash", "deploy/update.sh", "v9.9.9"], cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert "readiness policy: onboarding-close-only (from ./.env) · UPDATE_HOLD_ON_FAILURE=1" in r.stdout, r.stdout + r.stderr
    assert "another deploy is running" in r.stdout
    r = subprocess.run(["bash", "deploy/update.sh", "v9.9.9"], cwd=root, env=dict(env, STOIC_READINESS_POLICY="release-ready"), capture_output=True, text=True, timeout=60)
    assert "readiness policy: release-ready (from environment)" in r.stdout            # explicit env wins over the file
    (root / ".env").write_text("APP_ENV=production\n"); (root / "backend" / ".env").write_text("APP_ENV=production\nSTOIC_READINESS_POLICY=onboarding-close-only\n")
    r = subprocess.run(["bash", "deploy/update.sh", "v9.9.9"], cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert "readiness policy: onboarding-close-only (from ./.env)" in r.stdout          # backend/.env honoured too
    src = open(os.path.join(ROOT, "deploy", "update.sh")).read()
    assert "remove the line once Admin" in src and "unset STOIC_READINESS_POLICY" in open(os.path.join(ROOT, "deploy", "lib.sh")).read()


def test_compose_up_no_build_after_registry_pull_and_truth_folder_cleared(tmp_path):
    root, env = _world(tmp_path, fetch_ok=False)
    r = _run(root, env, "provision_images >/dev/null; images_from_registry; echo IFR=$?; compose_up 2>/dev/null; echo CU=$?")
    assert "IFR=0" in r.stdout, r.stdout + r.stderr
    assert any("compose" in ln and "up" in ln and "--no-build" in ln for ln in _log(env).splitlines())   # M120-2: auto mode after a pull
    # a build (fallback) must not pass --no-build
    (tmp_path / "docker.log").unlink()
    r = _run(root, dict(env, STUB_PULL_RC="1"), "provision_images >/dev/null; images_from_registry; echo IFR=$?; compose_up 2>/dev/null; echo CU=$?")
    assert "IFR=1" in r.stdout and not any("--no-build" in ln for ln in _log(env).splitlines() if " up" in ln)
    # tidy: the host truth folder never keeps the previous release's attestation after verify_attestation of an untagged build
    pub = root / "deploy" / "state" / "release"; pub.mkdir(parents=True, exist_ok=True)
    (pub / "attestation.current.json").write_text('{"commit": "old"}')
    r = _run(root, env, "verify_attestation; echo VA=$?")
    assert "VA=1" in r.stdout and not (pub / "attestation.current.json").exists()


def test_frontend_uses_same_origin_backend_url(tmp_path):
    for f, needle in (("components/InfraWizard.jsx", "iwr '${BACKEND_URL}/api/infra/agent/bootstrap/installer"),
                      ("pages/StatusPage.jsx", "const API = `${BACKEND_URL}/api`")):
        s = open(os.path.join(ROOT, "frontend", "src", f)).read()
        assert needle in s and 'BACKEND_URL } from "@/lib/api"' in s and "process.env.REACT_APP_BACKEND_URL}" not in s, f
    gi = open(os.path.join(ROOT, ".gitignore")).read()
    assert "release/ledger-anchors.jsonl" in gi and "deploy/state/key_ages.json" in gi and "deploy/state/release/" in gi
    tracked = subprocess.run(["git", "-C", ROOT, "ls-files", "release/ledger-anchors.jsonl", "deploy/state/key_ages.json"], capture_output=True, text=True).stdout
    assert tracked.strip() == ""
    lock = json.load(open(os.path.join(ROOT, "release", "rc_lock.json")))
    assert lock["signer_key_id"] == "stoic-release-ed25519-v2"


def test_release_not_finished_refuses_tag_in_auto_mode_unless_overridden(tmp_path):
    # the fetch wrapper fails with the "not finished" code 5 (no release / asset missing) or the outage code 3
    root, env = _world(tmp_path, env_lines=("APP_ENV=production", "GITHUB_REPO=x/y", "ATTESTATION_REQUIRED=false"), attested=False)
    wrapper = root / "scripts" / "release_attestation.py"
    wrapper.write_text(wrapper.read_text().replace('print("fetch failed: https://api.github.com/... <urlopen error>", file=sys.stderr); sys.exit(3)',
                                                   'print("fetch failed", file=sys.stderr); sys.exit(int(os.environ.get("STUB_FETCH_RC", "3")))'))
    r = _run(root, dict(env, STUB_FETCH_RC="5"), "verify_attestation; echo VA=$?; echo STATE=$ATTESTATION_FETCH_STATE")
    assert "VA=1" in r.stdout and "STATE=not-finished" in r.stdout, r.stdout + r.stderr
    assert "Release workflow for v9.9.9 not finished" in r.stdout and "--allow-unattested" in r.stdout
    r = _run(root, dict(env, STUB_FETCH_RC="5", STOIC_ALLOW_UNATTESTED="1"), "verify_attestation; echo VA=$?")
    assert "VA=0" in r.stdout and "--allow-unattested: Release workflow for v9.9.9 not finished — building an UNATTESTED copy" in r.stdout   # explicit override
    r = _run(root, dict(env, STUB_FETCH_RC="3"), "verify_attestation; echo VA=$?; echo STATE=$ATTESTATION_FETCH_STATE")
    assert "VA=0" in r.stdout and "STATE=unreachable" in r.stdout                               # outage never blocks
    r = _run(root, dict(env, STUB_FETCH_RC="5"), "sed -i 's/^ATTESTATION_REQUIRED=false$//' .env; verify_attestation; echo VA=$?")
    assert "VA=1" in r.stdout and "not finished" in r.stdout                                    # required mode: same clear message


def test_attestation_tooling_crash_is_reported_with_real_reason(tmp_path):
    # M121-4 — the VPS case: system python 3.9 → `str | None` TypeError at import → fetch exit 1 looked like "unreachable"
    root, env = _world(tmp_path, env_lines=("APP_ENV=production", "GITHUB_REPO=x/y", "ATTESTATION_REQUIRED=false"), attested=False)
    (root / "scripts" / "release_attestation.py").write_text(
        "import sys\nraise TypeError(\"unsupported operand type(s) for |: 'type' and 'NoneType'\")\n")
    r = _run(root, env, "verify_attestation; echo VA=$?; echo STATE=$ATTESTATION_FETCH_STATE; provision_images >/dev/null; echo RC=$?")
    assert "VA=0" in r.stdout and "STATE=error" in r.stdout and "RC=0" in r.stdout, r.stdout + r.stderr
    assert "attestation tooling failed (exit 1): TypeError: unsupported operand" in r.stdout
    assert "!! registry path unavailable: release_attestation.py fetch crashed (exit 1: TypeError" in r.stdout
    assert "no attested images for this ref" not in r.stdout
    src = _src(root)
    assert src["source"] == "build" and "fetch crashed (exit 1: TypeError: unsupported operand" in src["reason"] and "python3 3." in src["reason"]


def test_host_python_compat_gate(tmp_path):
    gate = os.path.join(ROOT, "scripts", "check_host_python_compat.py")
    r = subprocess.run([sys.executable, gate], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0 and "ok    scripts/release_attestation.py" in r.stdout and "backend/release_signing.py" in r.stdout, r.stdout + r.stderr
    for f in ("release_attestation.py", "verify_admission.py", "release_consistency_check.py", "signer_probe.py", "sync_env_examples.py", "verify_ea_release.py"):
        assert "from __future__ import annotations" in open(os.path.join(ROOT, "scripts", f)).read(), f
    for f in ("release_signing.py", "model_manifest.py"):
        assert "from __future__ import annotations" in open(os.path.join(ROOT, "backend", f)).read(), f
    assert "check_host_python_compat.py" in open(os.path.join(ROOT, ".github", "workflows", "ci.yml")).read()
    # the gate really detects the failure class (a `str | None` def) under the running interpreter when it is < 3.10;
    # under newer interpreters we at least prove it runs the discovered scripts
    py39 = shutil.which("python3.9") or (os.path.exists("/tmp/venv39/bin/python") and "/tmp/venv39/bin/python")
    if py39:
        r = subprocess.run([py39, gate], capture_output=True, text=True, cwd=ROOT, timeout=180)
        assert r.returncode == 0, r.stdout + r.stderr
        bad = tmp_path / "deploy"; bad.mkdir(); (tmp_path / "scripts").mkdir()
        (bad / "x.sh").write_text("python3 scripts/bad_sig.py\n")
        (tmp_path / "scripts" / "bad_sig.py").write_text("def f(x: str | None) -> list[str]:\n    return []\n")
        (tmp_path / "scripts" / "check_host_python_compat.py").write_text(open(gate).read()); (tmp_path / "backend").mkdir()
        r = subprocess.run([py39, str(tmp_path / "scripts" / "check_host_python_compat.py")], capture_output=True, text=True, timeout=120)
        assert r.returncode == 1 and "FAIL  scripts/bad_sig.py: TypeError" in r.stdout, r.stdout + r.stderr


def test_release_attestation_fetch_exit_codes(monkeypatch, tmp_path):
    import importlib, urllib.error
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    ra = importlib.import_module("release_attestation")

    class A:
        repo, tag, dest = "x/y", "v9.9.9", str(tmp_path / "d")

    def http(code):
        def _gh(url, accept):
            raise urllib.error.HTTPError(url, code, "x", {}, None)
        return _gh
    monkeypatch.setattr(ra, "_gh", http(404)); assert ra.cmd_fetch(A()) == 5
    monkeypatch.setattr(ra, "_gh", http(403)); assert ra.cmd_fetch(A()) == 6
    monkeypatch.setattr(ra, "_gh", http(502)); assert ra.cmd_fetch(A()) == 3
    monkeypatch.setattr(ra, "_gh", lambda u, a: (_ for _ in ()).throw(urllib.error.URLError("down"))); assert ra.cmd_fetch(A()) == 3
    monkeypatch.setattr(ra, "_gh", lambda u, a: json.dumps({"assets": []}).encode()); assert ra.cmd_fetch(A()) == 5   # release exists, no assets yet


def test_update_reprovision_flag_and_nothing_to_publish_hint(tmp_path):
    root, env = _world(tmp_path, env_lines=("APP_ENV=production", "GITHUB_REPO=x/y", "ATTESTATION_REQUIRED=false",
                                            f"BACKUP_PASSPHRASE_FILE={tmp_path}/pass"), attested=False)
    (tmp_path / "pass").write_text("p\n"); (root / "secrets").mkdir()
    (root / "deploy" / "backup.sh").write_text("#!/usr/bin/env bash\necho backup-stub\n")
    wrapper = root / "scripts" / "release_attestation.py"
    wrapper.write_text(wrapper.read_text().replace('sys.exit(3)', 'sys.exit(5)'))
    git = (tmp_path / "bin" / "git"); git.write_text(git.read_text().replace('"rev-parse HEAD") cat backend/BUILD_SHA ;;',
                                                                             '"rev-parse HEAD") cat backend/BUILD_SHA ;;\n  "rev-parse v9.9.9^{commit}") cat backend/BUILD_SHA ;;\n  "status --porcelain") exit 0 ;;'))
    r = subprocess.run(["bash", "deploy/update.sh", "v9.9.9", "--yes"], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "nothing to publish" in r.stdout and "--reprovision" in r.stdout, r.stdout + r.stderr
    r = subprocess.run(["bash", "deploy/update.sh", "v9.9.9", "--yes", "--reprovision"], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert "== reprovisioning" in r.stdout and "backup-stub" in r.stdout, r.stdout + r.stderr
    assert "Release workflow for v9.9.9 not finished" in r.stdout and "release refused before build" in r.stdout and r.returncode == 1
    up = open(os.path.join(ROOT, "deploy", "update.sh")).read()
    assert 'if [ "${REPROVISION}" = 1 ]; then' in up and "DEPLOY_MODE_OVERRIDE=build provision_images || true" in up        # rollback of a reprovision = on-host build


def test_key_ages_generated_at_is_utc_and_fingerprint_in_image():
    pre = open(os.path.join(ROOT, "deploy", "preflight.sh")).read()
    assert "iso(time.time())" in pre and "utcnow().timestamp()" not in pre
    assert "COPY release/release_key.fingerprint /app/release/release_key.fingerprint" in open(os.path.join(ROOT, "Dockerfile.backend")).read()
    assert "datetime.utcnow()" not in open(os.path.join(ROOT, "deploy", "lib.sh")).read()


def test_rollback_and_doctor_wiring():
    rb = open(os.path.join(ROOT, "deploy", "rollback.sh")).read()
    assert 'elif [ "$(deploy_mode)" = "auto" ]' in rb and "adopt_release_lock ||" in rb and "provision_images ||" in rb
    up = open(os.path.join(ROOT, "deploy", "update.sh")).read()
    assert '[ "$(deploy_mode)" = "build" ] || verify_attestation' in up           # auto-rollback re-fetches PREV's digests
    doc = open(os.path.join(ROOT, "deploy", "doctor.sh")).read()
    assert 'hdr "key & certificate ages' in doc and "rotate in %d d" in doc and "Origin CA certificate: expires" in doc
    dep = open(os.path.join(ROOT, "docs", "DEPLOYMENT.md")).read()
    assert "### Registry deploys (default since v1.60.12" in dep and "read:packages" in dep and "registry-fallback-build" in dep
    fp = [ln.split() for ln in open(os.path.join(ROOT, "release", "release_key.fingerprint")) if ln.strip() and not ln.startswith("#")]
    assert [(p[0], p[2]) for p in fp] == [("stoic-release-ed25519-v2", "current"), ("stoic-release-ed25519-v1", "revoked")]
    for f in ("lib.sh", "update.sh", "rollback.sh", "doctor.sh"):
        assert subprocess.run(["bash", "-n", os.path.join(ROOT, "deploy", f)]).returncode == 0, f
