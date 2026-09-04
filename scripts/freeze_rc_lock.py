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


def build_lock(prev=None):
    prev = prev or {}
    freeze = _pip_freeze()
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _run(["git", "-C", ROOT, "rev-parse", "HEAD"]),
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
    args = ap.parse_args()

    prev = None
    if os.path.exists(DEST):
        prev = json.load(open(DEST))

    if args.check:
        if not prev:
            print(f"FAIL: no lock at {DEST} — run scripts/freeze_rc_lock.py")
            return 1
        diffs = _drift(prev, build_lock(prev))
        if diffs:
            print("FAIL: environment drifted from RC lock:")
            for d in diffs:
                print(f"  · {d}")
            return 1
        print("OK: environment matches release/rc_lock.json")
        return 0

    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    lock = build_lock(prev)
    with open(DEST, "w") as f:
        json.dump(lock, f, indent=2)
    print(f"wrote {DEST}")
    print(f"  python {lock['python']['version']} · "
          f"{len(lock['python']['packages'])} packages · "
          f"node {lock['node']['version']} · "
          f"mongo {lock['mongodb']['version']} · "
          f"playwright {lock['playwright']['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
