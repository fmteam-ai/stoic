#!/usr/bin/env python3
"""Freeze / verify the Release-Candidate dependency lock.

    python scripts/freeze_rc_lock.py            # write release/rc_lock.json
    python scripts/freeze_rc_lock.py --check    # fail on any drift

Captures the EXACT toolchain so "it passed on our CI machine" is never an
explanation: Python, pip package pins, Node, yarn + yarn.lock hash,
MongoDB, Playwright, plus externally-attested fields (Windows build,
MetaEditor/MT5 build) that the Windows compile campaign fills in via
scripts/verify_ea_release.py.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "--root" in sys.argv:                      # r26 P1-01: hash the STAGED tree that is actually packaged
    ROOT = os.path.abspath(sys.argv[sys.argv.index("--root") + 1])
DEST = os.path.join(ROOT, "release", "rc_lock.json")


def _run(cmd):
    try:
        return subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return None


def _sha256(path):
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pip_freeze():
    out = _run([sys.executable, "-m", "pip", "freeze", "--all"]) or ""
    return sorted(ln for ln in out.splitlines() if ln and not ln.startswith("#"))


def _mongo_version():
    out = _run(["mongod", "--version"]) or ""
    m = re.search(r"db version v([\d.]+)", out)
    return m.group(1) if m else None


def _playwright_version():
    pkg = os.path.join(ROOT, "e2e", "package.json")
    if os.path.exists(pkg):
        deps = json.load(open(pkg))
        for scope in ("devDependencies", "dependencies"):
            v = (deps.get(scope) or {}).get("@playwright/test")
            if v:
                return v
    return _run(["npx", "--no-install", "playwright", "--version"])


def _tree_sha256(path):
    """Deterministic digest over a directory (relative path + content, sorted)."""
    if not os.path.isdir(path):
        return None
    h = hashlib.sha256()
    for root, _, names in sorted(os.walk(path)):
        for n in sorted(names):
            if "__pycache__" in root or n.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(root, n)
            h.update(os.path.relpath(full, path).encode() + b"\0" + bytes.fromhex(_sha256(full)))
    return h.hexdigest()


def build_lock(prev=None, *, commit=None, backend_digest=None, frontend_digest=None,
               target=None):
    """Bind the lock to the immutable release commit (round 9 P1-06): source SHA,
    image digests, dependency locks, migrations, frontend asset digest, test
    manifest, signer key id and deployment target — generated ONLY in CI from
    the release commit (`--commit`), never from a developer checkout."""
    prev = prev or {}
    freeze = _pip_freeze()
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    try:
        from release_signing import key_id as _kid
        signer_key_id = _kid(os.environ)
    except Exception:
        signer_key_id = None
    dist = next((d for d in (os.path.join(ROOT, "frontend", "dist"), os.path.join(ROOT, "frontend", "build"))
                 if os.path.isdir(d)), None)
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": commit or _run(["git", "-C", ROOT, "rev-parse", "HEAD"]),
        "source_sha": commit or _run(["git", "-C", ROOT, "rev-parse", "HEAD"]),
        "images": {"backend": backend_digest, "frontend": frontend_digest},
        "deployment_target": target,
        "authoritative": bool(commit and target),
        "note": ("CI-generated from the immutable release commit" if commit and target else
                 "developer snapshot — NOT release evidence; release.yml regenerates the lock bound to the tagged commit"),
        "signer_key_id": signer_key_id,
        "migrations_sha256": _tree_sha256(os.path.join(ROOT, "backend", "migrations")),
        "frontend_asset_digest": _tree_sha256(dist) if dist else None,
        "test_manifest_sha256": _sha256(os.path.join(ROOT, "docs", "TEST_MANIFEST.md")),
        "model_manifest_sha256": (_sha256(os.path.join(ROOT, "backend", "models_store", "MODEL_MANIFEST.json"))
                                  if os.path.exists(os.path.join(ROOT, "backend", "models_store", "MODEL_MANIFEST.json")) else None),
        "python": {
            "version": ".".join(map(str, sys.version_info[:3])),
            "packages": freeze,
            "packages_sha256": hashlib.sha256(
                "\n".join(freeze).encode()).hexdigest(),
            "requirements_txt_sha256": _sha256(
                os.path.join(ROOT, "backend", "requirements.txt")),
        },
        "node": {
            "version": _run(["node", "--version"]),
            "yarn": _run(["yarn", "--version"]),
            "yarn_lock_sha256": _sha256(
                os.path.join(ROOT, "frontend", "yarn.lock")),
        },
        "mongodb": {"version": _mongo_version()},
        "playwright": {"version": _playwright_version()},
        # Externally attested by the Windows compile campaign
        # (scripts/verify_ea_release.py --attest-*). Never guessed here.
        "windows_build_attested": (prev.get("windows_build_attested")
                                   or {"windows_build": None,
                                       "metaeditor_version": None,
                                       "mt5_build": None,
                                       "attested_at": None}),
    }


def _drift(lock, current):
    """Compare the environment-identity fields; returns list of mismatches."""
    diffs = []
    checks = [
        ("python.version", lock["python"]["version"],
         current["python"]["version"]),
        ("python.packages_sha256", lock["python"]["packages_sha256"],
         current["python"]["packages_sha256"]),
        ("python.requirements_txt_sha256",
         lock["python"]["requirements_txt_sha256"],
         current["python"]["requirements_txt_sha256"]),
        ("node.version", lock["node"]["version"], current["node"]["version"]),
        ("node.yarn_lock_sha256", lock["node"]["yarn_lock_sha256"],
         current["node"]["yarn_lock_sha256"]),
        ("mongodb.version", lock["mongodb"]["version"],
         current["mongodb"]["version"]),
        ("playwright.version", lock["playwright"]["version"],
         current["playwright"]["version"]),
    ]
    for name, want, have in checks:
        if want != have:
            diffs.append(f"{name}: locked={want!r} current={have!r}")
    return diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="verify current env matches the lock; exit 1 on drift")
    ap.add_argument("--commit", help="immutable release commit SHA (CI only)")
    ap.add_argument("--backend-digest")
    ap.add_argument("--frontend-digest")
    ap.add_argument("--target", help="deployment target, e.g. production")
    ap.add_argument("--out", help="write the lock somewhere other than release/rc_lock.json")
    ap.add_argument("--root", help="staged release tree to hash (default: this checkout)")
    ap.add_argument("--evidence", action="append", default=[], metavar="NAME=PATH",
                    help="r26-b P1-02: bind release evidence (SBOMs, admission record) by sha256 into the lock")
    args = ap.parse_args()
    dest = args.out or DEST

    prev = None
    if os.path.exists(dest):
        prev = json.load(open(dest))

    if args.check:
        if not prev:
            print(f"FAIL: no lock at {dest} — run scripts/freeze_rc_lock.py")
            return 1
        diffs = _drift(prev, build_lock(prev, commit=args.commit))
        if args.commit and prev.get("git_commit") != args.commit:
            diffs.append(f"git_commit: locked={prev.get('git_commit')!r} release={args.commit!r}")
        if diffs:
            print("FAIL: environment drifted from RC lock:")
            for d in diffs:
                print(f"  · {d}")
            return 1
        print(f"OK: environment matches {dest}")
        return 0

    # r26 P1-01: never freeze a lock over a stale test inventory — the manifest must
    # describe the final test files of THIS tree before its digest is bound
    stale = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                         "generate_test_manifest.py"), "--root", ROOT, "--check"],
                           capture_output=True, text=True)
    if stale.returncode != 0:
        print("FAIL: docs/TEST_MANIFEST.md is stale for this tree — regenerate it (scripts/generate_test_manifest.py) "
              "BEFORE freezing the RC lock; refusing to bind a test-manifest digest that does not match the packaged tests")
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    lock = build_lock(prev, commit=args.commit, backend_digest=args.backend_digest,
                      frontend_digest=args.frontend_digest, target=args.target)
    evidence = {}
    for item in args.evidence:
        name, _, path = item.partition("=")
        if not name or not os.path.isfile(path):
            print(f"FAIL: --evidence {item}: file missing — an authoritative lock binds every evidence artifact")
            return 1
        evidence[name] = _sha256(path)
    if evidence:
        lock["evidence"] = evidence
    with open(dest, "w") as f:
        json.dump(lock, f, indent=2)
    print(f"wrote {dest}")
    print(f"  python {lock['python']['version']} · "
          f"{len(lock['python']['packages'])} packages · "
          f"node {lock['node']['version']} · "
          f"mongo {lock['mongodb']['version']} · "
          f"playwright {lock['playwright']['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
