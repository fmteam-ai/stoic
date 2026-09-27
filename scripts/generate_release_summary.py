#!/usr/bin/env python3
"""Generate docs/RELEASE_SUMMARY.md from CURRENT evidence (audit round 11 P2-04).

Handoff/release claims must never be hand-written: build SHA, keepalive,
test count, open findings and the readiness verdict are read from the
repository and configuration. `--check` fails when the committed summary is
stale (used as a verify_release.sh step).
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "RELEASE_SUMMARY.md")
OPEN_FINDINGS = os.path.join(ROOT, "docs", "open_findings.json")


def _sha():
    return subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip()


def _keepalive():
    m = re.search(r'"--timeout-keep-alive",\s*"(\d+)"', open(os.path.join(ROOT, "Dockerfile.backend")).read())
    return int(m.group(1)) if m else None


def _tests():
    m = re.search(r"(\d[\d,]*) tests", open(os.path.join(ROOT, "docs", "TEST_MANIFEST.md")).read())
    return m.group(1) if m else "unknown"


def _open_findings():
    return json.load(open(OPEN_FINDINGS)) if os.path.exists(OPEN_FINDINGS) else []


def _lock():
    p = os.path.join(ROOT, "release", "rc_lock.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def render() -> str:
    sha, ka, tests, lock, findings = _sha(), _keepalive(), _tests(), _lock(), _open_findings()
    verdict = "NOT RELEASABLE" if (not lock.get("authoritative") or any(f["severity"] in ("P0", "P1") for f in findings)) else "RELEASE CANDIDATE"
    lines = [
        "# Release summary (GENERATED — do not edit; `python scripts/generate_release_summary.py`)",
        "",
        f"- Source commit: `{sha}`",
        f"- rc_lock: `{lock.get('git_commit')}` authoritative={lock.get('authoritative')} model_manifest={str(lock.get('model_manifest_sha256'))[:12]}…",
        f"- Uvicorn keepalive (container): {ka}s (application cap 300s)",
        f"- Test manifest: {tests} tests (docs/TEST_MANIFEST.md)",
        f"- Readiness verdict: **{verdict}**",
        "",
        "## Open findings (docs/open_findings.json)",
    ]
    lines += [f"- {f['id']} [{f['severity']}] {f['title']} — owner: {f.get('owner', 'engineering')}" for f in findings] or ["- none"]
    lines += ["", f"_generated {datetime.now(timezone.utc).isoformat()} — regenerate on every release commit_", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    text = render()
    # commit/lock lines are re-derived in CI on the release commit; claims (keepalive,
    # tests, verdict, findings) must match exactly.
    strip = lambda t: re.sub(r"(_generated .*_|- Source commit: .*|- rc_lock: .*)", "", t)
    if a.check:
        cur = open(OUT).read() if os.path.exists(OUT) else ""
        if strip(cur) != strip(text):
            print("STALE: docs/RELEASE_SUMMARY.md does not match current evidence — regenerate")
            return 1
        print("OK: release summary matches current evidence")
        return 0
    open(OUT, "w").write(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
