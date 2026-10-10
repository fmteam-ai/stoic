"""M117-4 — deploy/update.sh saves + discards hot-patched tracked files before `git checkout <tag>` (the deployed tree
must equal the signed release; a dirty tree used to abort AFTER the backup with "local changes would be overwritten")."""
import os
import subprocess

import pytest

from tests.unit.test_update_preflight_shell import ROOT

pytestmark = pytest.mark.unit


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _extract_block():
    upd = open(os.path.join(ROOT, "deploy", "update.sh")).read()
    start = upd.index('  DIRTY=$(git status --porcelain')
    end = upd.index('  git checkout --detach "${REF}"', start)
    return upd[start:end]


def test_update_sh_saves_patch_and_discards_local_edits_before_checkout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "deploy").mkdir(); (repo / "deploy" / "preflight.sh").write_text("echo v1\n"); (repo / "README").write_text("r\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "v1"); _git(repo, "tag", "v1")
    (repo / "deploy" / "preflight.sh").write_text("echo v2\n"); _git(repo, "commit", "-qam", "v2"); _git(repo, "tag", "v2")
    _git(repo, "checkout", "-q", "--detach", "v1")
    (repo / "deploy" / "preflight.sh").write_text("echo HOTPATCH\n")          # operator hot-patch on the host
    (repo / "deploy" / "host-prereqs.sh").write_text("echo ADDED\n")
    _git(repo, "add", "deploy")                                                 # … and STAGED (the real ded5552 case: `M `/`A `)
    (repo / "README").write_text("unstaged edit\n")
    (repo / "untracked.txt").write_text("keep me\n")                            # untracked files are never touched
    script = "set -euo pipefail\nREF=v2\n" + _extract_block() + '  git checkout --detach "${REF}"\n'
    r = subprocess.run(["bash", "-c", script], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "local modifications to tracked files saved to deploy/releases/local-changes-" in r.stdout and "deploy/preflight.sh" in r.stdout
    assert (repo / "deploy" / "preflight.sh").read_text() == "echo v2\n" and _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "v2")
    assert not (repo / "deploy" / "host-prereqs.sh").exists() and (repo / "README").read_text() == "r\n"
    patches = list((repo / "deploy" / "releases").glob("local-changes-*.patch"))
    txt = patches[0].read_text()
    assert len(patches) == 1 and "+echo HOTPATCH" in txt and "+echo ADDED" in txt and "+unstaged edit" in txt
    assert (repo / "untracked.txt").read_text() == "keep me\n"


def test_update_sh_clean_tree_writes_no_patch(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    _git(repo, "init", "-q", "-b", "main"); _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "f").write_text("1\n"); _git(repo, "add", "."); _git(repo, "commit", "-qm", "v1"); _git(repo, "tag", "v1")
    script = "set -euo pipefail\nREF=v1\n" + _extract_block()
    r = subprocess.run(["bash", "-c", script], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0 and "local modifications" not in r.stdout and not (repo / "deploy" / "releases").exists()
    upd = open(os.path.join(ROOT, "deploy", "update.sh")).read()
    assert upd.index("restore_tracked_release_files\n  # M117-4") < upd.index('  DIRTY=$(git status') < upd.index('  git checkout --detach "${REF}"')
