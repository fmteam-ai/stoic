#!/usr/bin/env python3
"""Capture the exact EA / Host Agent / strategy / policy hashes for release
evidence (docs/RELEASE_HASHES.json). Image digests + MSI signature are only
produced by the CI runners and are marked pending here."""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_source(p):
    """MQ5 source hash independent of line endings (the Windows runner checks out CRLF,
    Linux/preview LF — the recorded RC hash must match on both)."""
    with open(p, "rb") as f:
        data = f.read()
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def host_agent_hash():
    """Identical walk to release.yml manifest generation."""
    ha = hashlib.sha256()
    n = 0
    base = os.path.join(ROOT, "host_agent")
    for root, _, names in sorted(os.walk(base)):
        for name in sorted(names):
            p = os.path.join(root, name)
            rel = os.path.relpath(p, ROOT)
            ha.update(rel.replace(os.sep, "/").encode() + b"\0"
                      + bytes.fromhex(sha256_file(p)))
            n += 1
    return ha.hexdigest(), n


def merge_ea_entry(existing: dict | None, captured: dict) -> dict:
    """v1.60.41 — the `ea` entry is written by the ea-release workflow (signed EX5 record); a capture must
    NEVER discard it. Same MQ5 → keep the signed record untouched. New MQ5 → the unsigned capture takes
    the entry and the signed record is carried as `previous`, so verify_ea_release.record() signs
    previous = <stable release> into the new record and the fleet's stable EX5 stays accepted."""
    ex = existing or {}
    signed = bool(ex.get("ex5_sha256") and (ex.get("signature") or {}).get("sig_hex"))
    if not signed:
        if isinstance(ex.get("previous"), dict) and ex["previous"].get("signature"):
            captured["previous"] = ex["previous"]          # keep an already carried signed previous
        return captured
    if ex.get("version") == captured["version"] and ex.get("mq5_sha256") == captured["mq5_sha256"]:
        return ex
    prev = {k: v for k, v in ex.items() if k != "previous"}
    pp = ex.get("previous") or None
    if pp:
        prev["previous"] = {"version": pp.get("version"), "ex5_sha256": pp.get("ex5_sha256")}
    captured["previous"] = prev
    return captured


def main():
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    mq5 = os.path.join(ROOT, "backend/static/EmergentTradingBridge.mq5")
    ea_version = None
    for line in open(mq5, encoding="utf-8", errors="ignore"):
        if line.strip().startswith("#property version"):
            ea_version = line.split('"')[1] if '"' in line else None
            break
    ex5 = os.path.join(ROOT, "backend/static/EmergentTradingBridge.ex5")
    ha_hash, ha_files = host_agent_hash()

    from modules.pamm.strategy_guard import GUARD_VERSION
    from execution_authority import EXECUTION_POLICY_VERSION
    from strategies.registry import REGISTRY, strategy_hash

    out = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": commit,
        "ea": {"version": ea_version, "mq5_sha256": sha256_source(mq5),
               "ex5_sha256": (sha256_file(ex5) if os.path.exists(ex5)
                              else None),
               "ex5_note": None if os.path.exists(ex5) else
               "compiled ONLY by the release.yml MetaEditor gate on a real "
               "Windows runner — pending CI"},
        "host_agent": {"source_hash": ha_hash, "files": ha_files,
                       "msi_note": "MSI built + Authenticode-signed only in "
                                   "msi-release.yml — pending CI"},
        "policies": {"guard_policy_version": GUARD_VERSION,
                     "execution_policy_version":
                         str(EXECUTION_POLICY_VERSION)},
        "strategies": {sid: {"version": d.version,
                             "hash": strategy_hash(d)}
                       for sid, d in sorted(REGISTRY.items())},
        "images": {"backend_digest": None, "frontend_digest": None,
                   "note": "immutable digests + cosign signatures are "
                           "produced/verified only by release.yml on GHCR "
                           "— pending CI"},
    }
    dest = os.path.join(ROOT, "docs/RELEASE_HASHES.json")
    existing = json.load(open(dest)).get("ea") if os.path.exists(dest) else None
    out["ea"] = merge_ea_entry(existing, out["ea"])
    with open(dest, "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))
    print(f"\nwrote {dest}")


if __name__ == "__main__":
    main()
