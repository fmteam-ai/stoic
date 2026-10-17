#!/usr/bin/env python3
"""v1.60.61 hotfix - the ea-release commit step must never push an EX5 without its signed record.

Run 39030924128 (commit 585d2ce9) staged only the freshly compiled 1.66 EX5 while docs/RELEASE_HASHES.json#ea and
release/ea_release.json still recorded 1.65 (record() had crashed, pwsh kept going). This gate runs between `git add`
and `git commit`: the staged set must be CONSISTENT - EX5 on disk == recorded ex5_sha256 in BOTH records, all three
versions == the MQ5 #property version, and the record signed. Exit 0 = commit, 1 = refuse (nothing is pushed).
ASCII-only output (cp1252 consoles)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX5 = "backend/static/EmergentTradingBridge.ex5"
MQ5 = "backend/static/EmergentTradingBridge.mq5"
EA_RECORD = "release/ea_release.json"
HASHES = "docs/RELEASE_HASHES.json"


def sha256_file(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def mq5_version(path: str) -> str | None:
    try:
        m = re.search(r'^#property\s+version\s+"([\d.]+)"', open(path, encoding="utf-8", errors="replace").read(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def _load(path: str) -> dict | None:
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return None


def check(staged: list[str], ex5_sha: str | None, record: dict | None, hashes_ea: dict | None, version: str | None) -> list[str]:
    """Pure gate -> list of problems (empty = consistent)."""
    problems: list[str] = []
    ex5_staged = EX5 in staged
    if ex5_staged:
        for p in (EA_RECORD, HASHES):
            if p not in staged:
                problems.append(f"{EX5} is staged but {p} is not - an EX5 must never be committed without its record")
    if not (ex5_staged or EA_RECORD in staged or HASHES in staged):
        return problems
    if not version:
        problems.append(f"cannot read #property version from {MQ5}")
    for label, rec in ((EA_RECORD, record), (f"{HASHES}#ea", hashes_ea)):
        if not rec:
            problems.append(f"{label} missing or unreadable")
            continue
        if version and str(rec.get("version")) != str(version):
            problems.append(f"{label} records EA {rec.get('version')} but the MQ5 is {version}")
        if not rec.get("ex5_sha256"):
            problems.append(f"{label} has no ex5_sha256 (unsigned capture) - the record step did not run")
        elif ex5_sha and str(rec.get("ex5_sha256")).lower() != ex5_sha.lower():
            problems.append(f"{label} ex5_sha256 {str(rec.get('ex5_sha256'))[:16]}... != EX5 on disk {ex5_sha[:16]}...")
        if not (rec.get("signature") or {}).get("sig_hex"):
            problems.append(f"{label} is UNSIGNED")
    if not ex5_sha:
        problems.append(f"{EX5} missing on disk")
    return problems


def staged_paths(repo: str) -> list[str]:
    out = subprocess.run(["git", "-C", repo, "diff", "--cached", "--name-only"], capture_output=True, text=True, check=True).stdout
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=ROOT)
    ap.add_argument("--staged", action="store_true", help="read the staged file list from git (default when no --files)")
    ap.add_argument("--files", nargs="*", help="explicit list of changed paths (tests / dry runs)")
    a = ap.parse_args()
    staged = a.files if a.files is not None else staged_paths(a.repo)
    hashes = _load(os.path.join(a.repo, HASHES)) or {}
    problems = check(staged, sha256_file(os.path.join(a.repo, EX5)), _load(os.path.join(a.repo, EA_RECORD)),
                     hashes.get("ea"), mq5_version(os.path.join(a.repo, MQ5)))
    if problems:
        print("REFUSED: ea-release commit would be inconsistent (EX5 vs record):\n" + "\n".join(f" - {p}" for p in problems))
        print("Nothing committed. Fix the record step (verify_ea_release.py --sign must succeed) and re-run ea-release.")
        return 1
    print(f"OK: staged ea-release set is consistent ({', '.join(staged) or 'nothing staged'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
