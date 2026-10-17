#!/usr/bin/env python3
"""Regenerate the release artifacts in the ONE correct order (v1.60.35 "lock freeze guard"):

    docs/TEST_MANIFEST.md  →  release/rc_lock.json (binds the manifest hash)  →  docs/RELEASE_SUMMARY.md

Run this locally after adding/removing tests. CI deliberately does NOT run it: `test_rc_lock_manifest_bound` and the
`--check` steps must still FAIL on a stale manifest or lock — never auto-re-freeze in CI or silently in a generator.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STEPS = [
    ["scripts/generate_test_manifest.py"],
    ["scripts/freeze_rc_lock.py"],
    ["scripts/generate_release_summary.py"],
    ["scripts/generate_test_manifest.py", "--check"],   # the summary must not have moved the manifest the lock binds
]


def main() -> int:
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        print("regen_release_artifacts: refusing to run in CI — artifacts must be committed, CI only verifies them")
        return 2
    for step in STEPS:
        print(f"-- {' '.join(step)}")
        rc = subprocess.call([sys.executable, *step], cwd=ROOT)
        if rc != 0:
            print(f"!! step failed ({rc}): {' '.join(step)}")
            return rc
    print("release artifacts regenerated — commit docs/TEST_MANIFEST.md, docs/RELEASE_SUMMARY.md and release/rc_lock.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
