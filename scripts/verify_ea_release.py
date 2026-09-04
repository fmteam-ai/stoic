#!/usr/bin/env python3
"""MQL5 release verification chain (audit item 45).

    exact MQ5 → Windows MetaEditor → 0 errors → EX5 → SHA-256 →
    Ed25519 signature → release manifest

Record mode (run AFTER compiling on the Windows box, from the repo root):
    python scripts/verify_ea_release.py \
        --ex5 backend/static/EmergentTradingBridge.ex5 \
        --compile-log compile.log \
        --metaeditor-version "5.00 build 4620" \
        --windows-build "10.0.22631" --mt5-build "4620" --sign

Check mode (CI / readiness gate):
    python scripts/verify_ea_release.py --check

The gate REFUSES when: the compile log reports errors, the MQ5 on disk does
not hash-match the recorded RC source, the EX5 hash is missing, or the
Ed25519 signature does not verify against the published release key.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
HASHES = os.path.join(ROOT, "docs", "RELEASE_HASHES.json")
MQ5 = os.path.join(ROOT, "backend", "static", "EmergentTradingBridge.mq5")


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical_payload(ea: dict) -> bytes:
    body = {k: ea.get(k) for k in
            ("version", "mq5_sha256", "ex5_sha256", "metaeditor_version",
             "windows_build", "mt5_build")}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _parse_compile_log(path):
    """MetaEditor log → (errors, warnings). Refuses unparseable logs."""
    text = open(path, encoding="utf-8", errors="ignore").read()
    m = (re.search(r"[Rr]esult:?\s*(\d+)\s*errors?,?\s*(\d+)\s*warnings?", text)
         or re.search(r"(\d+)\s*error\(s\),\s*(\d+)\s*warning\(s\)", text))
    if not m:
        raise SystemExit("FAIL: cannot find the 'Result: N errors, M warnings' "
                         f"line in {path} — is this a MetaEditor compile log?")
    return int(m.group(1)), int(m.group(2))


def record(args):
    errors, warnings = _parse_compile_log(args.compile_log)
    if errors != 0:
        raise SystemExit(f"FAIL: compile log reports {errors} error(s) — "
                         "the chain requires 0 errors.")
    doc = json.load(open(HASHES))
    ea = doc.get("ea") or {}
    mq5_now = sha256_file(MQ5)
    if ea.get("mq5_sha256") and ea["mq5_sha256"] != mq5_now:
        raise SystemExit(
            "FAIL: backend/static/EmergentTradingBridge.mq5 does not match the "
            "recorded RC source hash — you compiled a DIFFERENT MQ5.\n"
            f"  recorded: {ea['mq5_sha256']}\n  on disk:  {mq5_now}")
    ea.update({
        "mq5_sha256": mq5_now,
        "ex5_sha256": sha256_file(args.ex5),
        "ex5_note": None,
        "compile_log": {"errors": errors, "warnings": warnings,
                        "log_sha256": sha256_file(args.compile_log)},
        "metaeditor_version": args.metaeditor_version,
        "windows_build": args.windows_build,
        "mt5_build": args.mt5_build,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    })
    if args.sign:
        from release_signing import KEY_ID, sign_hex
        ea["signature"] = {"key_id": KEY_ID,
                           "sig_hex": sign_hex(_canonical_payload(ea)),
                           "signed_at": datetime.now(timezone.utc).isoformat()}
    doc["ea"] = ea
    json.dump(doc, open(HASHES, "w"), indent=2)
    # Mirror the attested toolchain into the RC lock (audit item 42).
    lock_path = os.path.join(ROOT, "release", "rc_lock.json")
    if os.path.exists(lock_path):
        lock = json.load(open(lock_path))
        lock["windows_build_attested"] = {
            "windows_build": args.windows_build,
            "metaeditor_version": args.metaeditor_version,
            "mt5_build": args.mt5_build,
            "attested_at": ea["verified_at"]}
        json.dump(lock, open(lock_path, "w"), indent=2)
    print(f"OK: EX5 recorded — sha256 {ea['ex5_sha256'][:16]}… "
          f"({errors} errors / {warnings} warnings)"
          + (" · signed" if args.sign else " · UNSIGNED (rerun with --sign)"))
    return 0


def check_entry(ea: dict) -> list:
    """Shared verifier — returns a list of failures (empty = pass)."""
    fails = []
    if not ea.get("ex5_sha256"):
        fails.append("no EX5 hash recorded — MQL5 is externally UNVERIFIED "
                     "(compile on Windows, then scripts/verify_ea_release.py)")
    if os.path.exists(MQ5) and ea.get("mq5_sha256"):
        if sha256_file(MQ5) != ea["mq5_sha256"]:
            fails.append("MQ5 source drifted since the recorded compile — "
                         "re-run the Windows compile chain for this exact RC")
    log = ea.get("compile_log") or {}
    if ea.get("ex5_sha256") and log.get("errors") not in (0,):
        fails.append("recorded compile log is missing or reported errors")
    sig = ea.get("signature") or {}
    if not sig.get("sig_hex"):
        fails.append("EX5 entry is UNSIGNED")
    else:
        try:
            from release_signing import verify_hex
            if not verify_hex(_canonical_payload(ea), sig["sig_hex"]):
                fails.append("Ed25519 signature does NOT verify")
        except Exception as e:  # noqa: BLE001
            fails.append(f"signature verification unavailable: {e}")
    return fails


def check(_args):
    if not os.path.exists(HASHES):
        raise SystemExit("FAIL: docs/RELEASE_HASHES.json missing — run "
                         "scripts/capture_release_hashes.py first")
    ea = json.load(open(HASHES)).get("ea") or {}
    fails = check_entry(ea)
    if fails:
        print("FAIL: EA release verification chain incomplete:")
        for f in fails:
            print(f"  · {f}")
        return 1
    print(f"OK: EX5 {ea['ex5_sha256'][:16]}… — 0 compile errors, "
          f"signature verified (key {ea['signature'].get('key_id')})")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--ex5")
    ap.add_argument("--compile-log")
    ap.add_argument("--metaeditor-version")
    ap.add_argument("--windows-build")
    ap.add_argument("--mt5-build")
    ap.add_argument("--sign", action="store_true")
    args = ap.parse_args()
    if args.check:
        return check(args)
    if not (args.ex5 and args.compile_log):
        ap.error("record mode needs --ex5 and --compile-log (or use --check)")
    return record(args)


if __name__ == "__main__":
    sys.exit(main())
