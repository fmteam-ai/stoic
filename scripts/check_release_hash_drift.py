#!/usr/bin/env python3
"""CI guard — the MQ5 must never change without a matching RELEASE_HASHES capture.

The ea-release compile-record-sign job refuses to record an EX5 when the MQ5 on disk
differs from `docs/RELEASE_HASHES.json#ea.mq5_sha256`; discovering that only on the
Windows runner (minutes later, on main) is the surprise this guard removes. It fails in
the first CI job when:
  • the MQ5 sha256 != the recorded `mq5_sha256`           → re-run scripts/capture_release_hashes.py
  • the recorded `version` != the MQ5 `#property version`  → bump + re-capture
  • a signed EX5 record exists but was made for a DIFFERENT MQ5 (release/ea_release.json)
Exit 0 = consistent.
"""
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MQ5 = os.path.join(ROOT, "backend", "static", "EmergentTradingBridge.mq5")
HASHES = os.path.join(ROOT, "docs", "RELEASE_HASHES.json")
EA_RELEASE = os.path.join(ROOT, "release", "ea_release.json")
FIX = "fix: bump the EA version if the source changed, then `python scripts/capture_release_hashes.py` and commit docs/RELEASE_HASHES.json"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def mq5_version(path: str = MQ5) -> str | None:
    m = re.search(r'^#property\s+version\s+"([\d.]+)"', open(path, encoding="utf-8", errors="replace").read(), re.M)
    return m.group(1) if m else None


def check(mq5: str = MQ5, hashes: str = HASHES, ea_release: str = EA_RELEASE) -> list[str]:
    fails: list[str] = []
    on_disk = sha256_file(mq5)
    ea = (json.load(open(hashes)).get("ea") or {}) if os.path.exists(hashes) else {}
    if not ea.get("mq5_sha256"):
        fails.append(f"no ea.mq5_sha256 recorded in {os.path.relpath(hashes, ROOT)} — {FIX}")
    elif ea["mq5_sha256"] != on_disk:
        fails.append("MQ5 source drifted from the recorded RC hash\n"
                     f"  recorded: {ea['mq5_sha256']}\n  on disk:  {on_disk}\n  {FIX}")
    v = mq5_version(mq5)
    if ea.get("version") and v and ea["version"] != v:
        fails.append(f"recorded EA version {ea['version']} != MQ5 #property version {v} — {FIX}")
    if os.path.exists(ea_release):
        rec = json.load(open(ea_release))
        if rec.get("ex5_sha256") and rec.get("mq5_sha256") and rec["mq5_sha256"] != on_disk:
            fails.append("release/ea_release.json holds a signed EX5 for a DIFFERENT MQ5 — "
                         "push the MQ5 change to main so ea-release re-compiles and re-signs it")
    return fails


def main() -> int:
    fails = check()
    if fails:
        print("FAIL: EA release-hash drift guard\n" + "\n".join(f" • {f}" for f in fails))
        return 1
    print(f"OK: MQ5 {mq5_version()} matches docs/RELEASE_HASHES.json ({sha256_file(MQ5)[:16]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
