"""v1.60.47 — CI guard for protected release files (PR #98: an out-of-sync export reverted release/ea_release.json,
deleted backend/static/EmergentTradingBridge.ex5 and every policies/*.json; CI stayed green)."""
import json
import os
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import check_protected_paths as cpp  # noqa: E402

HUMAN = ("Dev", "dev@example.com")
BOT = (cpp.BOT_NAME, cpp.BOT_EMAIL)


def _git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True).stdout.strip()


def _commit(repo, msg, author=HUMAN, write=None, delete=()):
    for path, content in (write or {}).items():
        full = os.path.join(repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))
    for path in delete:
        os.remove(os.path.join(repo, path))
    _git(repo, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
           "GIT_COMMITTER_NAME": author[0], "GIT_COMMITTER_EMAIL": author[1]}
    subprocess.run(["git", "-C", repo, "commit", "-q", "-m", msg], check=True, env=env)
    return _git(repo, "rev-parse", "HEAD")


def _hashes(ea_version, extra="x"):
    return {"captured_at": extra, "ea": {"version": ea_version, "ex5_sha256": "ab" * 32}, "images": {}}


@pytest.fixture
def repo(tmp_path):
    r = str(tmp_path)
    _git(r, "init", "-q", "-b", "main")
    base = _commit(r, "main @ a2482c58-like", author=BOT, write={
        cpp.EX5: "EX5-BINARY", cpp.EA_RECORD: {"version": "1.65", "previous": {"version": "1.64"}},
        cpp.HASHES: _hashes("1.65"), "policies/demo-rules-v1.json": {"v": 1}, "policies/demo-rules-v2.json": {"v": 2},
        "release/policy_migrations/demo-1x1-v1.json": {"m": 1}, "backend/x.py": "print(1)\n"})
    return r, base


def test_pr_98_shape_is_rejected_with_one_violation_per_file(repo):
    r, base = repo
    head = _commit(r, "conflict_151026_2251 export", write={
        cpp.EA_RECORD: {"version": "1.60", "previous": None}, "backend/x.py": "print(2)\n"},
        delete=[cpp.EX5, "policies/demo-rules-v1.json", "policies/demo-rules-v2.json",
                "release/policy_migrations/demo-1x1-v1.json"])
    problems = cpp.check(r, base, head)
    joined = "\n".join(problems)
    assert cpp.EX5 in joined and "demo-rules-v1.json" in joined and "demo-rules-v2.json" in joined
    assert "demo-1x1-v1.json" in joined
    assert f"changed {cpp.EA_RECORD}" in joined
    assert len(problems) == 6  # 4 deletions + human write of ea_release.json + human write (delete) of the EX5


def test_bot_commit_may_write_ea_record_hashes_ea_and_ex5(repo):
    r, base = repo
    head = _commit(r, "ea-release: signed EX5 record [skip ci]", author=BOT, write={
        cpp.EX5: "EX5-1.66", cpp.EA_RECORD: {"version": "1.66"}, cpp.HASHES: _hashes("1.66")})
    assert cpp.check(r, base, head) == []


def test_human_changing_release_hashes_ea_is_rejected_but_other_keys_are_fine(repo):
    r, base = repo
    ok = _commit(r, "capture image hashes", write={cpp.HASHES: _hashes("1.65", extra="later")})
    assert cpp.check(r, base, ok) == []
    bad = _commit(r, "bump ea by hand", write={cpp.HASHES: _hashes("1.66", extra="later")})
    problems = cpp.check(r, base, bad)
    assert len(problems) == 1 and f"{cpp.HASHES}#ea" in problems[0]


def test_human_editing_ea_record_or_ex5_is_rejected_even_without_deletion(repo):
    r, base = repo
    head = _commit(r, "tweak", write={cpp.EA_RECORD: {"version": "1.65", "previous": None}, cpp.EX5: "other"})
    problems = cpp.check(r, base, head)
    assert any(f"changed {cpp.EA_RECORD}" in p for p in problems)
    assert any(f"changed {cpp.EX5}" in p for p in problems)


def test_spoofed_bot_email_with_wrong_name_is_not_the_bot(repo):
    r, base = repo
    head = _commit(r, "fake", author=("Dev", cpp.BOT_EMAIL), write={cpp.EA_RECORD: {"version": "1.60"}})
    assert cpp.check(r, base, head)


def test_unrelated_changes_and_policy_additions_pass(repo):
    r, base = repo
    head = _commit(r, "rules-policy: demo-rules-v3", write={
        "policies/demo-rules-v3.json": {"v": 3}, "backend/x.py": "print(3)\n"})
    assert cpp.check(r, base, head) == []


def test_cli_exit_codes(repo):
    r, base = repo
    script = os.path.join(ROOT, "scripts", "check_protected_paths.py")
    bad = _commit(r, "rm ex5", delete=[cpp.EX5])
    res = subprocess.run([sys.executable, script, "--repo", r, "--base", base, "--head", bad], capture_output=True, text=True)
    assert res.returncode == 1 and "::error::" in res.stdout and "re-sync the branch with main" in res.stdout
    res = subprocess.run([sys.executable, script, "--repo", r, "--base", "0" * 40], capture_output=True, text=True)
    assert res.returncode == 0 and "no base commit" in res.stdout


def test_ci_has_a_blocking_protected_paths_job():
    wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "ci.yml")))
    job = wf["jobs"]["protected-paths"]
    assert any(s.get("with", {}).get("fetch-depth") == 0 for s in job["steps"] if "uses" in s)
    run = next(s for s in job["steps"] if "run" in s)
    assert "scripts/check_protected_paths.py" in run["run"]
    assert "github.event.pull_request.base.sha" in run["env"]["BASE"]
    assert "continue-on-error" not in job and "continue-on-error" not in run
