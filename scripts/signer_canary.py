#!/usr/bin/env python3
"""Release-signer canary (audit round 9 P1-01): sign a fresh nonce through the
CONFIGURED signer and verify it against the pinned public key. Emits a JSON
evidence record consumed by release_attestation.py (`--signer-canary`).

    python scripts/signer_canary.py --out evidence/signer-canary.json [--require-external]

Exit 0 on PASS, 2 on FAIL, 3 on SKIPPED (no external signer configured and
--require-external not set).
"""
import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))


def run(require_external: bool) -> dict:
    import release_signing as rs
    env = os.environ
    rec = {"record": "stoic.signer-canary", "at": datetime.now(timezone.utc).isoformat(),
           "mode": rs._mode(env), "key_id": rs.key_id(env), "result": "FAIL"}
    viols = rs.signer_config_violations(env)
    if rec["mode"] != "external":
        if require_external:
            rec["error"] = "external signer required for release canary; mode=" + rec["mode"]
            return rec
        rec.update(result="SKIPPED", note="no external signer configured")
        return rec
    if viols:
        rec["error"] = viols[0]
        return rec
    health = rs.signer_health(env)
    rec["health"] = health
    if not health["ok"]:
        rec["error"] = health.get("error", "health check failed")
        return rec
    nonce = b"stoic-signer-canary:" + secrets.token_bytes(32)
    try:
        sig = rs.sign_hex(nonce)
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"sign failed: {e}"
        return rec
    ok = rs.verify_hex(nonce, sig, env["RELEASE_PUBLIC_KEY_B64"].strip())
    wrong = rs.verify_hex(nonce + b"x", sig, env["RELEASE_PUBLIC_KEY_B64"].strip())
    rec.update(nonce_sha256=__import__("hashlib").sha256(nonce).hexdigest(), signature_verified=ok,
               tamper_rejected=not wrong, result="PASS" if ok and not wrong else "FAIL")
    if rec["result"] != "PASS":
        rec["error"] = "signature did not verify against the pinned public key"
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="evidence/signer-canary.json")
    ap.add_argument("--require-external", action="store_true")
    a = ap.parse_args()
    rec = run(a.require_external)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    json.dump(rec, open(a.out, "w"), indent=2, sort_keys=True)
    print(json.dumps(rec, indent=2, sort_keys=True))
    return {"PASS": 0, "SKIPPED": 3}.get(rec["result"], 2)


if __name__ == "__main__":
    sys.exit(main())
