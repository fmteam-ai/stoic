"""v1.60.41 — check_ea_fleet_compat.py must gate against the EX5 the fleet actually runs: a rolled-back tag
(v1.60.40) never shipped, so deriving 'stable' from the previous v* tag failed the recovery PR."""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "backend"))
pytestmark = pytest.mark.unit

SHIPPED = "b05ed821" + "0" * 56          # EA 1.64 — live on the 4 terminals
NEVER_LIVE = "5481970e" + "0" * 56       # EA 1.65 record of the rolled-back v1.60.40


def _git(repo, *a):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, env=env, check=True).stdout.strip()


def _commit_record(repo, version, ex5, tag, previous=None):
    os.makedirs(os.path.join(repo, "release"), exist_ok=True)
    json.dump({"version": version, "ex5_sha256": ex5, "previous": previous}, open(os.path.join(repo, "release", "ea_release.json"), "w"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", tag or version, "--allow-empty")
    if tag:
        _git(repo, "tag", tag)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    import check_ea_fleet_compat as gate
    r = str(tmp_path / "repo")
    os.makedirs(r)
    _git(r, "init", "-q")
    _commit_record(r, "1.64", SHIPPED, "v1.60.39")
    _commit_record(r, "1.65", NEVER_LIVE, "v1.60.40")                       # signed previous: null — rolled back
    _commit_record(r, "1.65", "a" * 64, None, previous={"version": "1.64", "ex5_sha256": SHIPPED})   # recovery PR HEAD
    monkeypatch.setattr(gate, "ROOT", r)
    monkeypatch.setattr(gate, "ROLLED_BACK_FILE", os.path.join(r, "release", "rolled_back_tags.json"))
    monkeypatch.setattr(gate, "RELEASES_LOG", os.path.join(r, "deploy", "releases.log"))
    monkeypatch.delenv("EA_FLEET_STABLE_SHA", raising=False)
    return r, gate


def _args(**kw):
    import types
    d = {"stable_sha": None, "stable_record": None, "stable_ref": None}
    d.update(kw)
    return types.SimpleNamespace(**d)


def test_without_a_rollback_record_the_previous_tag_is_the_never_live_release(repo, capsys):
    r, gate = repo
    sha, desc = gate.stable_from_args(_args())
    assert sha == NEVER_LIVE and "v1.60.40" in desc                           # the failure mode the recovery PR hit


def test_tracked_rolled_back_tags_file_skips_the_never_live_tag(repo, capsys):
    r, gate = repo
    json.dump({"tags": {"v1.60.40": {"reason": "deploy rolled back 14 Oct"}}}, open(gate.ROLLED_BACK_FILE, "w"))
    sha, desc = gate.stable_from_args(_args())
    assert sha == SHIPPED and "v1.60.39" in desc
    assert "skipped rolled-back / never-shipped tag(s): v1.60.40 (deploy rolled back 14 Oct)" in capsys.readouterr().out


def test_host_releases_log_rollback_entries_skip_the_tag(repo):
    r, gate = repo
    sha40 = _git(r, "rev-parse", "--short", "v1.60.40")
    sha39 = _git(r, "rev-parse", "--short", "v1.60.39")
    os.makedirs(os.path.join(r, "deploy"))
    open(gate.RELEASES_LOG, "w").write(f"2026-10-14T10:00:00Z {sha39} ref=v1.60.39\n2026-10-14T19:00:00Z {sha40} ref=v1.60.40\n"
                                       f"2026-10-14T19:30:00Z {sha39} rollback-from={sha40}\n")
    assert gate.rolled_back_tags() == {"v1.60.40": f"deploy/releases.log: rolled back ({sha40})"}
    sha, desc = gate.stable_from_args(_args())
    assert sha == SHIPPED and "v1.60.39" in desc


def test_gate_refused_and_auto_rollback_entries_are_recognised(repo):
    r, gate = repo
    os.makedirs(os.path.join(r, "deploy"))
    open(gate.RELEASES_LOG, "w").write("2026-10-14T19:00:00Z abcdef0 gate-refused ref=v1.60.40\n"
                                       "2026-10-15T09:00:00Z abcdef0 auto-rollback-from=v1.60.40\n")
    assert "v1.60.40" in gate.rolled_back_tags()


def test_env_override_wins(repo, monkeypatch):
    r, gate = repo
    monkeypatch.setenv("EA_FLEET_STABLE_SHA", SHIPPED.upper())
    sha, desc = gate.stable_from_args(_args())
    assert sha == SHIPPED and "EA_FLEET_STABLE_SHA" in desc
    sha, desc = gate.stable_from_args(_args(stable_sha="c" * 64))
    assert sha == "c" * 64 and desc == "--stable-sha"


def test_recovery_pr_passes_the_gate_against_the_shipped_ex5(repo):
    r, gate = repo
    json.dump({"tags": {"v1.60.40": {"reason": "rolled back"}}}, open(gate.ROLLED_BACK_FILE, "w"))
    rec = json.load(open(os.path.join(r, "release", "ea_release.json")))
    sha, desc = gate.stable_from_args(_args())
    assert gate.check(rec, sha, desc, verify_previous=False) == []
    assert gate.check(rec, NEVER_LIVE, "v1.60.40", verify_previous=False)     # never-live EX5 would still block — hence the skip


def test_all_tags_rolled_back_means_nothing_to_compare(repo):
    r, gate = repo
    json.dump({"tags": {"v1.60.40": {}, "v1.60.39": {}}}, open(gate.ROLLED_BACK_FILE, "w"))
    assert gate.stable_from_args(_args()) == (None, "no earlier shipped v* tag")


def test_repo_records_v1_60_40_as_rolled_back_and_ci_passes_the_override():
    doc = json.load(open(os.path.join(ROOT, "release", "rolled_back_tags.json")))
    assert "v1.60.40" in doc["tags"] and "rolled back" in doc["tags"]["v1.60.40"]["reason"]
    for wf in ("ci.yml", "release.yml"):
        s = open(os.path.join(ROOT, ".github", "workflows", wf), encoding="utf-8").read()
        assert "EA_FLEET_STABLE_SHA: ${{ vars.EA_FLEET_STABLE_SHA }}" in s
