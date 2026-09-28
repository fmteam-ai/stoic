#!/usr/bin/env python3
"""Paired release ADMISSION manifest (audit r17 P2-01 / r18 P2-01).

The deployable unit is the backend+frontend digest PAIR. This file is created
in the release workflow right after the quarantine push (digests known), signed
with cosign, verified immediately, covered by SHA256SUMS and the attestation,
and REQUIRED by the deploy controller (deploy/lib.sh) — aliases/tags are only
convenience pointers.

  emit:   verify_admission.py --emit release-admission.json --backend-digest D1 --frontend-digest D2 --tag vX --commit SHA
  verify: verify_admission.py release-admission.json --backend-digest D1 --frontend-digest D2 [--tag vX] [--commit SHA]
Exit 0 = PASS.
"""
import argparse
import json
import re
import sys

DIGEST_RE = re.compile(r"^[a-z0-9.\-/_]+@sha256:[0-9a-f]{64}$")


def build(backend: str, frontend: str, tag: str, commit: str) -> dict:
    for d in (backend, frontend):
        if not DIGEST_RE.match(d or ""):
            raise SystemExit(f"not an immutable digest reference: {d!r}")
    return {"record": "release-admission", "version": 1, "tag": tag, "commit": commit,
            "images": {"backend": backend, "frontend": frontend},
            "rule": "deploy BOTH digests from this manifest or neither — never from a single tag"}


def verify(doc: dict, backend: str | None, frontend: str | None, tag: str | None, commit: str | None) -> list[str]:
    problems = []
    if doc.get("record") != "release-admission":
        problems.append("record is not release-admission")
    imgs = doc.get("images") or {}
    for name, want in (("backend", backend), ("frontend", frontend)):
        have = imgs.get(name) or ""
        if not DIGEST_RE.match(have):
            problems.append(f"{name} image is not digest-pinned: {have!r}")
        if want and have != want:
            problems.append(f"{name} digest mismatch: manifest {have} != expected {want}")
    if tag and doc.get("tag") != tag:
        problems.append(f"tag mismatch: {doc.get('tag')} != {tag}")
    if commit and doc.get("commit") != commit:
        problems.append(f"commit mismatch: {doc.get('commit')} != {commit}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--backend-digest")
    ap.add_argument("--frontend-digest")
    ap.add_argument("--tag")
    ap.add_argument("--commit")
    a = ap.parse_args()
    if a.emit:
        doc = build(a.backend_digest, a.frontend_digest, a.tag or "", a.commit or "")
        with open(a.file, "w") as f:
            json.dump(doc, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"admission manifest written: {a.file}")
        return 0
    doc = json.load(open(a.file))
    problems = verify(doc, a.backend_digest, a.frontend_digest, a.tag, a.commit)
    print(json.dumps({"file": a.file, "result": "PASS" if not problems else "FAIL", "problems": problems,
                      "images": doc.get("images")}, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
