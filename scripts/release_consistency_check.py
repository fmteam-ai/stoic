#!/usr/bin/env python3
"""Release provenance consistency gate (audit round 12 P1-02).

Every identifier embedded in a package must name ONE artifact:
  backend/BUILD_SHA · rc_lock git_commit/source_sha · model manifest code_commit
  · RELEASE_SUMMARY source commit · rc_lock test_manifest_sha256 vs the real
  docs/TEST_MANIFEST.md digest · rc_lock model_manifest_sha256 vs the real
  MODEL_MANIFEST.json digest · (optional) image digests · (optional) deployed
  /api/version build.

    python scripts/release_consistency_check.py [--root DIR] [--strict]
        [--commit SHA] [--backend-digest D] [--frontend-digest D] [--deployed-version-url URL]

Non-strict (developer snapshot, rc_lock.authoritative=false): digest fields must
match; commit fields are reported. Strict (release job, or automatically when the
lock is authoritative): EVERY field must match exactly → exit 1 on any mismatch,
printed as a concise per-field report (never a traceback).
"""
from __future__ import annotations   # host-side: AlmaLinux 9 ships Python 3.9 (no `str | None` at runtime)
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _sha256(p):
    if not os.path.exists(p):
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _json(p):
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return {}


def build_sha(root):
    p = os.path.join(root, "backend", "BUILD_SHA")
    try:
        v = open(p).read().strip().lower()
    except OSError:
        return None
    return v if SHA_RE.match(v) else "unsubstituted"


def summary_source(root):
    p = os.path.join(root, "docs", "RELEASE_SUMMARY.md")
    try:
        m = re.search(r"- Source commit: `([^`]+)`", open(p).read())
        return m.group(1) if m else None
    except OSError:
        return None


def gather(root, *, commit=None, backend_digest=None, frontend_digest=None, deployed_version_url=None):
    lock = _json(os.path.join(root, "release", "rc_lock.json"))
    mm_path = os.path.join(root, "backend", "models_store", "MODEL_MANIFEST.json")
    mm = _json(mm_path)
    facts = {
        "build_sha": build_sha(root),
        "lock.git_commit": lock.get("git_commit"),
        "lock.source_sha": lock.get("source_sha"),
        "lock.authoritative": lock.get("authoritative"),
        "lock.evidence": sorted((lock.get("evidence") or {}).keys()) or None,
        "model_manifest.code_commit": (mm.get("body") or {}).get("code_commit"),
        "model_manifest.resigned_from": (mm.get("body") or {}).get("resigned_from_commit"),
        "release_summary.source_commit": summary_source(root),
        "lock.test_manifest_sha256": lock.get("test_manifest_sha256"),
        "actual.test_manifest_sha256": _sha256(os.path.join(root, "docs", "TEST_MANIFEST.md")),
        "lock.model_manifest_sha256": lock.get("model_manifest_sha256"),
        "actual.model_manifest_sha256": _sha256(mm_path),
        "lock.images.backend": (lock.get("images") or {}).get("backend"),
        "lock.images.frontend": (lock.get("images") or {}).get("frontend"),
        "expected.commit": commit,
        "expected.images.backend": backend_digest,
        "expected.images.frontend": frontend_digest,
        "deployed.build": None,
    }
    if deployed_version_url:
        try:
            with urllib.request.urlopen(deployed_version_url, timeout=10) as r:
                body = json.loads(r.read().decode())
            facts["deployed.build"] = (body.get("build") or body.get("git_commit") or body.get("commit")
                                       or body.get("build_sha"))
        except Exception as e:  # noqa: BLE001
            facts["deployed.build"] = f"unreachable ({type(e).__name__})"
    return facts


REQUIRED_EVIDENCE = {"sbom_backend", "sbom_frontend", "admission"}


def compare(facts, *, strict):
    """→ (mismatches, warnings): lists of 'field: detail' strings."""
    mismatches, warnings = [], []

    def digest(a, b, name):
        if facts[a] != facts[b]:
            mismatches.append(f"{name}: lock {str(facts[a])[:12]} != actual {str(facts[b])[:12]}")
    digest("lock.test_manifest_sha256", "actual.test_manifest_sha256", "test_manifest_sha256")
    # v1.60.3 — release staging re-signs the model manifest for the release commit (resigned_from_commit set,
    # code_commit == build_sha): its digest can no longer equal the developer-snapshot lock. Strict verification of
    # the re-signed manifest happens in `model_manifest verify --build`; here it is a note, not a mismatch — unless
    # the lock is authoritative (then the lock was frozen AFTER staging and must match).
    resigned = (facts.get("model_manifest.resigned_from") and not strict
                and facts["model_manifest.code_commit"] == facts["build_sha"])
    if resigned and facts["lock.model_manifest_sha256"] != facts["actual.model_manifest_sha256"]:
        warnings.append(f"model_manifest_sha256: re-signed at release staging from {str(facts['model_manifest.resigned_from'])[:12]} "
                        f"(lock {str(facts['lock.model_manifest_sha256'])[:12]} is the developer snapshot)")
    else:
        digest("lock.model_manifest_sha256", "actual.model_manifest_sha256", "model_manifest_sha256")
    if facts["lock.git_commit"] != facts["lock.source_sha"]:
        mismatches.append("lock.source_sha: differs from lock.git_commit")
    ref = facts["expected.commit"] or facts["lock.git_commit"]
    commit_fields = ["build_sha", "lock.git_commit", "model_manifest.code_commit", "release_summary.source_commit"]
    if facts["deployed.build"] is not None:
        commit_fields.append("deployed.build")
    sink = mismatches if strict else warnings
    for f in commit_fields:
        if facts[f] != ref:
            sink.append(f"{f}: {str(facts[f])[:12]} != release commit {str(ref)[:12]}")
    for side in ("backend", "frontend"):
        exp = facts[f"expected.images.{side}"]
        if exp is not None and facts[f"lock.images.{side}"] != exp:
            mismatches.append(f"lock.images.{side}: {facts[f'lock.images.{side}']} != built {exp}")
        elif strict and not facts[f"lock.images.{side}"]:
            mismatches.append(f"lock.images.{side}: missing (authoritative lock requires image digests)")
    if strict and facts["lock.authoritative"] is not True:
        mismatches.append("lock.authoritative: must be true for a release")
    if strict:   # r26-b P1-02: SBOMs + signed admission record are part of the one-commit chain
        missing = sorted(REQUIRED_EVIDENCE - set(facts["lock.evidence"] or []))
        if missing:
            mismatches.append(f"lock.evidence: missing {', '.join(missing)} (authoritative lock binds SBOMs + admission)")
    return mismatches, warnings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--commit")
    ap.add_argument("--backend-digest")
    ap.add_argument("--frontend-digest")
    ap.add_argument("--deployed-version-url")
    a = ap.parse_args()
    facts = gather(a.root, commit=a.commit, backend_digest=a.backend_digest,
                   frontend_digest=a.frontend_digest, deployed_version_url=a.deployed_version_url)
    strict = a.strict or facts["lock.authoritative"] is True
    mismatches, warnings = compare(facts, strict=strict)
    mode = "STRICT" if strict else "developer-snapshot"
    for k, v in facts.items():
        print(f"  {k:36s} {v}")
    for w in warnings:
        print(f"WARN  {w}")
    for m in mismatches:
        print(f"FAIL  {m}")
    if mismatches:
        print(f"PROVENANCE INCONSISTENT ({mode}): {len(mismatches)} field(s) disagree")
        return 1
    print(f"OK: release provenance consistent ({mode}; {len(warnings)} developer-snapshot commit note(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
