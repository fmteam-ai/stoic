#!/usr/bin/env python3
"""v1.60.41 release gate — the release must ACCEPT the EX5 the fleet is running (the EA channel's stable).

14 Oct: a merge wiped the signed EA 1.64 record, ea-release signed 1.65 with previous: null, and the new
server rejected the stable 1.64 EX5 on all 4 terminals (DEMO attestations voided → CLOSE_ONLY). This gate
blocks that release before the tag / before the host rebuilds: the hashes the new record accepts
(current + signed previous) must include the stable EX5.

Stable EX5 sources (first that applies):
    --stable-sha <hex>            explicit (e.g. release_state.stable["stoic-ea-ex5"] from the server)
    --stable-record <path>        a release/ea_release.json the fleet runs (deploy/update.sh: the PREV checkout)
    --stable-ref <git ref>        git show <ref>:release/ea_release.json (release.yml: the previous v* tag)
    (default)                     the latest v* tag reachable from HEAD^ (no earlier tag → nothing to compare)
Exit 0 = compatible (or no stable to compare against — printed), 1 = RELEASE BLOCKED.
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
EA_RELEASE = os.path.join(ROOT, "release", "ea_release.json")
HASHES = os.path.join(ROOT, "docs", "RELEASE_HASHES.json")


def _load_record(path: str) -> dict:
    doc = json.load(open(path))
    return doc.get("ea") if "ea" in doc and "ex5_sha256" not in doc else doc


def accepted_hashes(rec: dict) -> list[str]:
    """What the server will accept from this record: current + previous (ea_capabilities.accepted_ea_sha256s)."""
    out = []
    for h in (rec.get("ex5_sha256"), (rec.get("previous") or {}).get("ex5_sha256")):
        h = str(h or "").lower()
        if len(h) == 64 and h not in out:
            out.append(h)
    return out


def previous_verifies(rec: dict) -> bool:
    """The server only honours `previous` when it verifies on its own signature — mirror that."""
    prev = rec.get("previous") or None
    if not prev:
        return False
    try:
        from release_signing import verify_hex, key_id_accepted, release_public_key_for
        from ea_capabilities import _canonical_payload
        kid = (prev.get("signature") or {}).get("key_id")
        sig = (prev.get("signature") or {}).get("sig_hex")
        if not (sig and prev.get("compiled_by") == "github-actions" and key_id_accepted(kid)):
            return False
        pub = release_public_key_for(kid)
        return bool(verify_hex(_canonical_payload(prev), sig, pub, purpose="ea-release")
                    or verify_hex(_canonical_payload(prev, legacy=True), sig, pub, purpose="ea-release"))
    except Exception:  # noqa: BLE001
        return False


def _git_show(ref: str, path: str = "release/ea_release.json") -> dict | None:
    try:
        raw = subprocess.check_output(["git", "show", f"{ref}:{path}"], cwd=ROOT, stderr=subprocess.DEVNULL)
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def _previous_tag() -> str | None:
    try:
        return subprocess.check_output(["git", "describe", "--tags", "--abbrev=0", "--match", "v*", "HEAD^"],
                                       cwd=ROOT, stderr=subprocess.DEVNULL).decode().strip() or None
    except Exception:  # noqa: BLE001
        return None


def stable_from_args(args) -> tuple[str | None, str]:
    """(stable ex5 sha256 or None, description)."""
    if args.stable_sha:
        return args.stable_sha.lower(), "--stable-sha"
    if args.stable_record:
        rec = _load_record(args.stable_record)
        return str(rec.get("ex5_sha256") or "").lower() or None, f"{args.stable_record} (EA {rec.get('version')})"
    ref = args.stable_ref or _previous_tag()
    if not ref:
        return None, "no earlier v* tag"
    rec = _git_show(ref)
    if not rec:
        return None, f"{ref} has no release/ea_release.json"
    return str(rec.get("ex5_sha256") or "").lower() or None, f"{ref} (EA {rec.get('version')})"


def check(rec: dict, stable_sha: str | None, stable_desc: str, *, verify_previous: bool = True) -> list[str]:
    fails: list[str] = []
    if not stable_sha:
        return fails
    acc = accepted_hashes(rec)
    prev = rec.get("previous") or {}
    if stable_sha not in acc:
        fails.append(f"fleet stable EX5 {stable_sha[:16]}… [{stable_desc}] is NOT accepted by this release record "
                     f"(current {str(rec.get('ex5_sha256') or '')[:16]}…, previous "
                     f"{str(prev.get('ex5_sha256') or 'null')[:16]}) — every terminal still on the stable EA would be "
                     "rejected (DEMO attestation voided, CLOSE_ONLY). The ea entry lost its signed previous record: "
                     "restore it and re-run ea-release.")
    elif stable_sha != str(rec.get("ex5_sha256") or "").lower() and verify_previous and not previous_verifies(rec):
        fails.append(f"fleet stable EX5 {stable_sha[:16]}… is only accepted via `previous` (EA {prev.get('version')}), "
                     "but that previous record does not verify on its own signature — the server will drop it.")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", default=EA_RELEASE if os.path.exists(EA_RELEASE) else HASHES)
    ap.add_argument("--stable-sha")
    ap.add_argument("--stable-record")
    ap.add_argument("--stable-ref")
    ap.add_argument("--no-verify-previous", action="store_true", help="skip the previous-signature check (no key pin available)")
    args = ap.parse_args()
    if not os.path.exists(args.record):
        print(f"RELEASE BLOCKED: {os.path.relpath(args.record, ROOT)} missing — run the ea-release workflow first")
        return 1
    rec = _load_record(args.record)
    stable, desc = stable_from_args(args)
    if not stable:
        print(f"OK: no fleet stable EX5 to compare against ({desc})")
        return 0
    from verify_ea_release import load_backend_env_pins
    load_backend_env_pins()                                   # host: the key pins live in backend/.env
    fails = check(rec, stable, desc, verify_previous=not args.no_verify_previous)
    if fails:
        print("RELEASE BLOCKED: EA fleet compatibility gate\n" + "\n".join(f" • {f}" for f in fails))
        return 1
    print(f"OK: release EA {rec.get('version')} accepts the fleet stable EX5 {stable[:16]}… [{desc}] "
          f"({'current' if stable == str(rec.get('ex5_sha256') or '').lower() else 'signed previous'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
