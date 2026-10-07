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
EA_RELEASE = os.path.join(ROOT, "release", "ea_release.json")   # compact signed record shipped in the image
RC_LOCK = os.path.join(ROOT, "release", "rc_lock.json")


def mq5_property_version(path=MQ5) -> str | None:
    m = re.search(r'#property\s+version\s+"([^"]+)"', open(path, encoding="utf-8", errors="ignore").read())
    return m.group(1) if m else None


def _read_log(path) -> str:
    raw = open(path, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or (len(raw) > 1 and raw[1:2] == b"\x00"):
        return raw.decode("utf-16", errors="ignore")      # MetaEditor writes UTF-16 logs
    return raw.decode("utf-8", errors="ignore")


def toolchain_from_log(path) -> dict:
    """MetaEditor identity straight from the compile log (never from a CLI claim alone)."""
    text = _read_log(path)
    me = re.search(r"MetaEditor\s+([\d.]+)\s+build\s+(\d+)", text)
    return {"metaeditor_version": f"{me.group(1)} build {me.group(2)}" if me else None,
            "mt5_build": me.group(2) if me else None}


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


def _canonical_payload(ea: dict, key_id: str | None = None) -> bytes:
    body = {k: ea.get(k) for k in
            ("version", "mq5_sha256", "ex5_sha256", "metaeditor_version",
             "windows_build", "mt5_build", "source_commit")}
    body["key_id"] = key_id or (ea.get("signature") or {}).get("key_id")   # N102-5 — key id is signed
    prev = ea.get("previous")
    if prev:   # N104-6 — the previous release is bound to the current record (mirror of ea_capabilities._canonical_payload)
        body["previous"] = {"version": prev.get("version"), "ex5_sha256": prev.get("ex5_sha256")}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _parse_compile_log(path):
    """MetaEditor log → (errors, warnings). Refuses unparseable logs."""
    text = _read_log(path)
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
    mq5_now = sha256_source(MQ5)
    if ea.get("mq5_sha256") and ea["mq5_sha256"] != mq5_now:
        raise SystemExit(
            "FAIL: backend/static/EmergentTradingBridge.mq5 does not match the "
            "recorded RC source hash — you compiled a DIFFERENT MQ5.\n"
            f"  recorded: {ea['mq5_sha256']}\n  on disk:  {mq5_now}")
    tool = toolchain_from_log(args.compile_log)
    if args.metaeditor_version and tool["metaeditor_version"] and args.metaeditor_version != tool["metaeditor_version"]:
        raise SystemExit(f"FAIL: --metaeditor-version '{args.metaeditor_version}' != compile log "
                         f"'{tool['metaeditor_version']}' — the log is the authority")
    version = mq5_property_version()
    if not version:
        raise SystemExit("FAIL: cannot read #property version from the MQ5")
    # N-R6 — keep the last SIGNED release as `previous` so terminals still on it stay
    # live-admissible during the rollout (the backend accepts current + previous).
    previous = ea.get("previous")
    if ea.get("ex5_sha256") and (ea.get("signature") or {}).get("sig_hex") and ea.get("version") != version:
        # N105-1 — the copy must stay verifiable on its OWN signature, which (N104-6) covers ITS
        # previous.{version, ex5_sha256}: keep that pointer (and nothing more) inside the copy.
        previous = {k: v for k, v in ea.items() if k != "previous"}
        pp = ea.get("previous") or None
        if pp:
            previous["previous"] = {"version": pp.get("version"), "ex5_sha256": pp.get("ex5_sha256")}
    ea.update({
        "version": version,
        "mq5_sha256": mq5_now,
        "ex5_sha256": sha256_file(args.ex5),
        "ex5_note": None,
        "previous": previous,
        "compile_log": {"errors": errors, "warnings": warnings,
                        "log_sha256": sha256_file(args.compile_log)},
        "metaeditor_version": tool["metaeditor_version"] or args.metaeditor_version,
        "windows_build": args.windows_build,
        "mt5_build": tool["mt5_build"] or args.mt5_build,
        "source_commit": (args.source_commit or os.environ.get("GITHUB_SHA") or "").lower() or None,
        "compiled_by": args.compiled_by or ("github-actions" if os.environ.get("GITHUB_ACTIONS") else "manual"),
        "verified_at": datetime.now(timezone.utc).isoformat(),
    })
    if args.sign:
        from release_signing import key_id as _kid, sign_hex
        kid = _kid(purpose="ea-release")
        ea["signature"] = {"key_id": kid,
                           "sig_hex": sign_hex(_canonical_payload(ea, kid), purpose="ea-release"),
                           "signed_at": datetime.now(timezone.utc).isoformat()}
    doc["ea"] = ea
    json.dump(doc, open(HASHES, "w"), indent=2)
    os.makedirs(os.path.dirname(EA_RELEASE), exist_ok=True)
    json.dump(ea, open(EA_RELEASE, "w"), indent=2)      # read by ea_capabilities.expected_ea_sha256()
    # Mirror the attested toolchain into the RC lock (audit item 42).
    lock_path = RC_LOCK
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
        if sha256_source(MQ5) != ea["mq5_sha256"]:
            fails.append("MQ5 source drifted since the recorded compile — "
                         "re-run the Windows compile chain for this exact RC")
    log = ea.get("compile_log") or {}
    if ea.get("ex5_sha256") and log.get("errors") not in (0,):
        fails.append("recorded compile log is missing or reported errors")
    if ea.get("ex5_sha256") and os.path.exists(MQ5) and ea.get("version") != mq5_property_version():
        fails.append(f"recorded EA version {ea.get('version')} != MQ5 #property version {mq5_property_version()}")
    if ea.get("ex5_sha256") and ea.get("compiled_by") not in ("github-actions",):
        fails.append("EX5 was not compiled by the sanctioned CI MetaEditor job (compiled_by != github-actions)")
    sig = ea.get("signature") or {}
    if not sig.get("sig_hex"):
        fails.append("EX5 entry is UNSIGNED")
    else:
        try:
            from release_signing import verify_hex
            if not verify_hex(_canonical_payload(ea), sig["sig_hex"], purpose="ea-release"):
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
    ap.add_argument("--source-commit", help="release commit the EX5 is bound to (default $GITHUB_SHA)")
    ap.add_argument("--compiled-by", help="sanctioned job identity (CI sets github-actions)")
    args = ap.parse_args()
    if args.check:
        return check(args)
    if not (args.ex5 and args.compile_log):
        ap.error("record mode needs --ex5 and --compile-log (or use --check)")
    return record(args)


if __name__ == "__main__":
    sys.exit(main())
