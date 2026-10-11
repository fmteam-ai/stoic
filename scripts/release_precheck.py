#!/usr/bin/env python3
"""O3 (Phase 1) — pre-backup release check for deploy/update.sh <tag>.

Before touching the host (backup → fetch → checkout) make sure the tag exists on origin and its Release
workflow run has CONCLUDED SUCCESSFULLY. The attestation gate later refuses an unfinished Release anyway, but
only after the backup and the checkout — the operator then sat on a half-updated tree.

Exit codes: 0 ok · 2 tag missing on origin · 3 Release run missing / running / failed · 4 GitHub API unreachable.
Host python is 3.9 (AlmaLinux 9): no modern syntax. Uses GITHUB_TOKEN when set (private repo)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


def tag_on_origin(tag: str) -> bool:
    out = subprocess.run(["git", "ls-remote", "--tags", "origin", "refs/tags/%s" % tag],
                         capture_output=True, text=True, timeout=60)
    return out.returncode == 0 and bool(out.stdout.strip())


def release_run(repo: str, tag: str, token: str | None) -> dict:
    url = "https://api.github.com/repos/%s/actions/workflows/release.yml/runs?per_page=20&event=push" % repo
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "stoic-update"})
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    for run in data.get("workflow_runs") or []:
        if run.get("head_branch") == tag:
            return {"found": True, "status": run.get("status"), "conclusion": run.get("conclusion"),
                    "url": run.get("html_url"), "sha": run.get("head_sha")}
    return {"found": False}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tag", required=True)
    a = ap.parse_args(argv)
    if not tag_on_origin(a.tag):
        print("!! release check: tag %s does not exist on origin — create the tag (git tag %s && git push origin %s) "
              "and wait for the Release workflow" % (a.tag, a.tag, a.tag))
        return 2
    try:
        run = release_run(a.repo, a.tag, os.environ.get("GITHUB_TOKEN") or None)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        print("!! release check: GitHub API unreachable (%s) — cannot confirm the Release workflow of %s" % (e, a.tag))
        return 4
    if not run["found"]:
        print("!! release check: no Release workflow run for %s yet — it starts when the tag is pushed; retry in a minute" % a.tag)
        return 3
    if run["status"] != "completed":
        print("!! release check: Release workflow for %s is %s (%s) — wait for it to finish, then re-run update.sh"
              % (a.tag, run["status"], run["url"]))
        return 3
    if run["conclusion"] != "success":
        print("!! release check: Release workflow for %s concluded %s (%s) — fix the release before deploying"
              % (a.tag, run["conclusion"], run["url"]))
        return 3
    print("   release check: %s exists on origin · Release workflow succeeded (%s)" % (a.tag, run["url"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
