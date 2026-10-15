"""v1.60.41 — deploy/rollback.sh after an update that HELD on failure: the adopted release files
(release/rc_lock.json, backend/BUILD_SHA, backend/models_store/MODEL_MANIFEST.json, docs/RELEASE_SUMMARY.md)
are tracked and modified, so `git checkout` refused and the rollback died. rollback.sh now calls
restore_tracked_release_files (as update.sh does) and saves/resets any other tracked edits before switching."""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit

ADOPTED = ("release/rc_lock.json", "backend/BUILD_SHA", "backend/models_store/MODEL_MANIFEST.json", "docs/RELEASE_SUMMARY.md")
STUB_LIB = """
. {real_lib}
deploy_mode() {{ echo build; }}
verify_attestation() {{ return 0; }}
adopt_release_lock() {{ echo "   (stub) adopt_release_lock"; return 0; }}
provision_images() {{ echo "   (stub) provision_images"; return 0; }}
compose_up() {{ echo "   (stub) compose_up"; return 0; }}
wait_api_health() {{ return 0; }}
verify_running_sha() {{ return 0; }}
verify_stack_images() {{ return 0; }}
wait_release_ready() {{ return 0; }}
"""


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=env, check=True).stdout.strip()


def _write(repo, rel, text):
    p = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(text)


def _repo(tmp_path, rollback_src: str):
    """A host checkout with two releases (v1 → v2) where the v2 update HELD on failure: the adopted
    release files are modified in the tree and releases.log lists v1 then v2."""
    repo = str(tmp_path / "host")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    for rel in ADOPTED + (".env.example", "backend/.env.example"):
        _write(repo, rel, f"{rel} @ v1\n")
    _write(repo, "deploy/rollback.sh", rollback_src)
    _write(repo, "deploy/lib.sh", STUB_LIB.format(real_lib=os.path.join(ROOT, "deploy", "lib.sh")))
    _write(repo, "deploy/backup.sh", "#!/usr/bin/env bash\necho '   (stub) backup'\n")
    os.chmod(os.path.join(repo, "deploy/backup.sh"), 0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "v1")
    v1 = _git(repo, "rev-parse", "--short", "HEAD")
    for rel in ADOPTED:
        _write(repo, rel, f"{rel} @ v2\n")
    _git(repo, "commit", "-qam", "v2")
    v2 = _git(repo, "rev-parse", "--short", "HEAD")
    _git(repo, "checkout", "-q", "--detach", v2)
    for rel in ADOPTED:                                   # adopt_release_lock wrote the authoritative lock → tracked files modified
        _write(repo, rel, f"{rel} ADOPTED authoritative for {v2}\n")
    _write(repo, "deploy/releases.log", f"2026-10-15T10:00:00Z {v1} ref=v1\n2026-10-15T11:00:00Z {v2} ref=v2 held-on-failure\n")
    assert _git(repo, "status", "--porcelain", "--untracked-files=no").count("\n") >= 3
    return repo, v1, v2


def _run_rollback(repo):
    return subprocess.run(["bash", "deploy/rollback.sh"], cwd=repo, capture_output=True, text=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x"))


def test_rollback_directly_after_a_held_update_switches_back_and_restores_the_release_files(tmp_path):
    repo, v1, v2 = _repo(tmp_path, open(os.path.join(ROOT, "deploy", "rollback.sh")).read())
    r = _run_rollback(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"rollback: {v2} -> {v1}" in r.stdout and f"rollback to {v1} complete" in r.stdout
    assert _git(repo, "rev-parse", "--short", "HEAD") == v1
    for rel in ADOPTED:
        assert open(os.path.join(repo, rel)).read() == f"{rel} @ v1\n"          # v1's tree, not the held v2 adoption
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert "error: Your local changes" not in r.stderr and "would be overwritten" not in r.stderr
    assert not os.path.exists(os.path.join(repo, "deploy", "releases")) or not os.listdir(os.path.join(repo, "deploy", "releases"))  # no patch needed for the adopted files
    log = open(os.path.join(repo, "deploy", "releases.log")).read().strip().splitlines()
    assert log[-1].split()[1] == v1 and f"rollback-from={v2}" in log[-1]


def test_rollback_saves_other_tracked_edits_as_a_patch_instead_of_refusing(tmp_path):
    repo, v1, v2 = _repo(tmp_path, open(os.path.join(ROOT, "deploy", "rollback.sh")).read())
    with open(os.path.join(repo, "deploy", "backup.sh"), "a") as f:
        f.write("# hot-patched by operator\n")
    r = _run_rollback(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _git(repo, "rev-parse", "--short", "HEAD") == v1
    assert "local modifications to tracked files saved to deploy/releases/local-changes-" in r.stdout
    patches = [f for f in os.listdir(os.path.join(repo, "deploy", "releases")) if f.startswith("local-changes-")]
    assert len(patches) == 1 and "hot-patched by operator" in open(os.path.join(repo, "deploy", "releases", patches[0])).read()


def test_without_the_restore_the_held_update_blocked_the_rollback(tmp_path):
    """Negative control — the pre-fix script (checkout without restoring the adopted files) is refused by git."""
    src = open(os.path.join(ROOT, "deploy", "rollback.sh")).read()
    start = src.index("restore_tracked_release_files\nDIRTY=")
    end = src.index('git checkout --detach "${REF}"', start)
    old = src[:start] + src[end:]
    assert "restore_tracked_release_files" not in old
    repo, v1, v2 = _repo(tmp_path, old)
    r = _run_rollback(repo)
    assert r.returncode != 0
    assert "would be overwritten by checkout" in r.stderr
    assert _git(repo, "rev-parse", "--short", "HEAD") == v2                     # stuck on the held release


def test_rollback_restores_before_switching_like_update_sh():
    rb = open(os.path.join(ROOT, "deploy", "rollback.sh")).read()
    assert rb.index("restore_tracked_release_files") < rb.index('git checkout --detach "${REF}"')
    assert rb.index("deploy/backup.sh backup") < rb.index("restore_tracked_release_files")   # safety backup first, then clean the tree
    shutil.which("bash")
