#!/usr/bin/env python3
"""Generate docs/RELEASE_SUMMARY.md from CURRENT evidence (audit rounds 11 P2-04 · 12 P2-01).

Handoff/release claims must never be hand-written: source identity, RC-lock
identity + authoritative flag + digests, keepalive, test count, open findings
and the readiness verdict are read from the repository/archive. `--check`
compares EVERY field (only the generated timestamp is normalized) and fails
with the exact field names that disagree.

Source identity comes from backend/BUILD_SHA (release archive / image) and
falls back to `git rev-parse HEAD` only in a developer checkout. In a
developer checkout with a NON-authoritative lock the committed summary can
never carry its own commit (self-reference), so `source_commit` is reported
but not failed there; everything else is strict everywhere.
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
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _sha():
    """BUILD_SHA (archive/image) → STOIC_BUILD_SHA → git HEAD (dev checkout) → 'unknown'."""
    try:
        v = open(os.path.join(ROOT, "backend", "BUILD_SHA")).read().strip().lower()
        if _SHA_RE.match(v):
            return v
    except OSError:
        pass
    v = (os.environ.get("STOIC_BUILD_SHA") or "").strip().lower()
    if _SHA_RE.match(v):
        return v
    try:
        return subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


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


def _ea_signed() -> bool:
    """A signed, CI-compiled EX5 record the shipped server trusts (fail closed)."""
    import sys
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    try:
        from ea_capabilities import accepted_ea_sha256s
        return bool(accepted_ea_sha256s()) and not os.environ.get("EA_RELEASE_SHA256")
    except Exception:  # noqa: BLE001
        return False


def fields() -> dict:
    lock, findings = _lock(), _open_findings()
    # A13 P0-02 — RELEASABLE only for an authoritative, digest-pinned lock with a signed EX5 record
    images = lock.get("images") or {}
    ea_record = _ea_signed()
    releasable = (lock.get("authoritative") and images.get("backend") and images.get("frontend") and ea_record
                  and not any(f["severity"] in ("P0", "P1") for f in findings))
    verdict = "RELEASABLE" if releasable else "NOT RELEASABLE"
    return {
        "source_commit": _sha(),
        "lock_commit": str(lock.get("git_commit")),
        "lock_authoritative": str(lock.get("authoritative")),
        "model_manifest_sha256": str(lock.get("model_manifest_sha256")),
        "test_manifest_sha256": str(lock.get("test_manifest_sha256")),
        "keepalive_seconds": str(_keepalive()),
        "test_count": str(_tests()),
        "verdict": verdict,
        "open_findings": "; ".join(f"{f['id']} [{f['severity']}] {f['title']} — owner: {f.get('owner', 'engineering')}"
                                   for f in findings) or "none",
    }


def render(f: dict) -> str:
    lines = [
        "# Release summary (GENERATED — do not edit; `python scripts/generate_release_summary.py`)",
        "",
        f"- Source commit: `{f['source_commit']}`",
        f"- rc_lock: `{f['lock_commit']}` authoritative={f['lock_authoritative']}",
        f"- Model manifest sha256: `{f['model_manifest_sha256']}`",
        f"- Test manifest sha256: `{f['test_manifest_sha256']}`",
        f"- Uvicorn keepalive (container): {f['keepalive_seconds']}s (application cap 300s)",
        f"- Test manifest: {f['test_count']} tests (docs/TEST_MANIFEST.md)",
        f"- Readiness verdict: **{f['verdict']}**",
        "",
        "## Open findings (docs/open_findings.json)",
    ]
    lines += [f"- {x.strip()}" for x in f["open_findings"].split(";")] if f["open_findings"] != "none" else ["- none"]
    lines += ["", f"_generated {datetime.now(timezone.utc).isoformat()} — regenerate on every release commit_", ""]
    return "\n".join(lines)


_PATTERNS = {
    "source_commit": r"- Source commit: `([^`]*)`",
    "lock_commit": r"- rc_lock: `([^`]*)` authoritative=",
    "lock_authoritative": r"authoritative=(\S+)",
    "model_manifest_sha256": r"- Model manifest sha256: `([^`]*)`",
    "test_manifest_sha256": r"- Test manifest sha256: `([^`]*)`",
    "keepalive_seconds": r"- Uvicorn keepalive \(container\): (\S+?)s ",
    "test_count": r"- Test manifest: (\S+) tests",
    "verdict": r"- Readiness verdict: \*\*([^*]+)\*\*",
}


def parse(text: str) -> dict:
    out = {k: (m.group(1) if (m := re.search(p, text)) else None) for k, p in _PATTERNS.items()}
    sect = text.split("## Open findings (docs/open_findings.json)", 1)
    body = sect[1] if len(sect) == 2 else ""
    items = [ln[2:].strip() for ln in body.splitlines() if ln.startswith("- ")]
    out["open_findings"] = "none" if items == ["none"] else "; ".join(items)
    return out


def check(current_text: str, f: dict, *, strict_source: bool) -> list:
    cur = parse(current_text)
    diffs = []
    for k, want in f.items():
        got = cur.get(k)
        if k == "open_findings":
            same = [x.strip() for x in str(got).split(";")] == [x.strip() for x in want.split(";")]
        else:
            same = got == want
        if not same:
            if k == "source_commit" and not strict_source:
                print(f"note  source_commit: summary {str(got)[:12]} vs checkout {str(want)[:12]} (developer snapshot — bound at release)")
                continue
            diffs.append(f"{k}: summary={str(got)[:40]!r} current={str(want)[:40]!r}")
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--strict", action="store_true", help="also fail on source_commit drift (release job / archive)")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    f = fields()
    if a.check:
        cur = open(OUT).read() if os.path.exists(OUT) else ""
        strict_source = a.strict or f["lock_authoritative"] == "True"
        diffs = check(cur, f, strict_source=strict_source)
        if diffs:
            print("STALE: docs/RELEASE_SUMMARY.md disagrees with current evidence on:")
            for d in diffs:
                print(f"  {d}")
            return 1
        print("OK: release summary matches current evidence")
        return 0
    open(a.out, "w").write(render(f))
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
