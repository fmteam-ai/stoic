#!/usr/bin/env python3
"""CI guard for the EA / policy release records (PR #98 incident, 2026-10-15).

A workspace export that was out of sync with GitHub main reverted release/ea_release.json to an old record,
deleted backend/static/EmergentTradingBridge.ex5 and deleted every signed policy. Nothing in CI noticed.

Fails when the BASE..HEAD range:
  * deletes/renames backend/static/EmergentTradingBridge.ex5, policies/*.json or release/policy_migrations/*.json
  * touches release/ea_release.json or backend/static/EmergentTradingBridge.ex5 in a commit not authored by the
    ea-release bot
  * changes the `ea` object of docs/RELEASE_HASHES.json in a commit not authored by the ea-release bot

Usage: python3 scripts/check_protected_paths.py --base <sha> --head <sha> [--repo .]
"""
import argparse
import fnmatch
import json
import os
import subprocess
import sys

EX5 = "backend/static/EmergentTradingBridge.ex5"
EA_RECORD = "release/ea_release.json"
HASHES = "docs/RELEASE_HASHES.json"
NEVER_DELETE = [EX5, "policies/*.json", "release/policy_migrations/*.json"]
BOT_ONLY = [EA_RECORD, EX5]
BOT_EMAIL = os.environ.get("EA_RELEASE_BOT_EMAIL", "ea-release@users.noreply.github.com")
BOT_NAME = os.environ.get("EA_RELEASE_BOT_NAME", "stoic-ea-release[bot]")
NULL_SHA = "0" * 40


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=check).stdout


def _protected_delete(path):
    return any(fnmatch.fnmatch(path, pat) for pat in NEVER_DELETE)


def _ea_of(repo, rev):
    try:
        raw = _git(repo, "show", f"{rev}:{HASHES}")
    except subprocess.CalledProcessError:
        return None
    try:
        return json.loads(raw).get("ea")
    except ValueError:
        return {"__unparseable__": True}


def check(repo, base, head):
    """Returns a list of violation strings (empty = pass)."""
    merge_base = _git(repo, "merge-base", base, head).strip()
    problems = []
    for line in _git(repo, "diff", "--name-status", "-M", merge_base, head).splitlines():
        parts = line.split("\t")
        status, old = parts[0], parts[1]
        if status.startswith("D") and _protected_delete(old):
            problems.append(f"DELETED protected file {old} — restore it; it only exists on main")
        elif status.startswith("R") and _protected_delete(old):
            problems.append(f"RENAMED protected file {old} -> {parts[2]} — not allowed")
    commits = _git(repo, "rev-list", "--reverse", f"{merge_base}..{head}").split()
    for sha in commits:
        name, email = _git(repo, "show", "-s", "--format=%an%n%ae", sha).splitlines()[:2]
        is_bot = email == BOT_EMAIL and name == BOT_NAME
        touched = _git(repo, "show", "--name-only", "--format=", sha).split()
        if is_bot:
            continue
        for p in BOT_ONLY:
            if p in touched:
                problems.append(f"{sha[:8]} ({name} <{email}>) changed {p} — only the ea-release workflow may write it")
        if HASHES in touched and _ea_of(repo, f"{sha}^") != _ea_of(repo, sha):
            problems.append(f"{sha[:8]} ({name} <{email}>) changed {HASHES}#ea — only the ea-release workflow may write it")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--repo", default=".")
    a = ap.parse_args()
    if not a.base or a.base == NULL_SHA:
        print("protected-paths: no base commit (first push / tag) — nothing to compare")
        return 0
    if subprocess.run(["git", "-C", a.repo, "cat-file", "-e", f"{a.base}^{{commit}}"], capture_output=True).returncode:
        print(f"protected-paths: base {a.base} not in this clone — fetch-depth: 0 required")
        return 2
    problems = check(a.repo, a.base, a.head)
    for p in problems:
        print(f"::error::{p}")
    if problems:
        print(f"protected-paths: {len(problems)} violation(s). {EA_RECORD} and {HASHES}#ea are written ONLY by the "
              "ea-release workflow; the EX5 and signed policies must never be deleted by a PR. Do not hand-edit — "
              "re-sync the branch with main.")
        return 1
    print("protected-paths: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
