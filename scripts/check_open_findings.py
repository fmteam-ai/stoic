#!/usr/bin/env python3
"""M119-7 — release discipline gate: a v* tag is refused while docs/open_findings.json lists an OPEN P0/P1 review
finding without an explicit, written `tag_waiver`. Waivers are printed (they end up in the Release run log and the
release summary) so a demo-only tag can ship while the pre-live infrastructure backlog is visible, never silent.

    python3 scripts/check_open_findings.py            # exit 1 on an un-waived P0/P1
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "docs", "open_findings.json")


def evaluate(findings):
    blocking, waived = [], []
    for f in findings:
        if f.get("severity") not in ("P0", "P1"):
            continue
        if f.get("status", "open") != "open":
            continue
        if (f.get("tag_waiver") or "").strip():
            waived.append(f)
        else:
            blocking.append(f)
    return blocking, waived


def main():
    findings = json.load(open(PATH))
    blocking, waived = evaluate(findings)
    for f in waived:
        print(f"waived  {f['severity']} {f['id']}: {f['title'][:80]} — waiver: {f['tag_waiver']}")
    for f in blocking:
        print(f"BLOCKING {f['severity']} {f['id']}: {f['title'][:80]} — add \"tag_waiver\" with the reason or close the finding")
    if blocking:
        print(f"release discipline: {len(blocking)} open P0/P1 finding(s) without a tag waiver — do not tag", file=sys.stderr)
        return 1
    print(f"release discipline: OK ({len(waived)} waived P0/P1 finding(s) printed above)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
