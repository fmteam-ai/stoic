#!/usr/bin/env python3
"""Probe a hosted STOIC release signer end to end (no API deploy needed).

    python scripts/signer_probe.py --url https://<signer-host> \
        --token <SIGNER_TOKEN> | --token-file <path> \
        --public-key <RELEASE_PUBLIC_KEY_B64>

Checks: /healthz liveness, /public-key equals the pinned key, authenticated
/health identity, and a sign → verify round-trip (plus tamper rejection) using
the SAME client code the API uses (backend/release_signing). Exit 0 = PASS.
"""
import argparse
import json
import os
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--token")
    ap.add_argument("--token-file")
    ap.add_argument("--public-key", required=True)
    ap.add_argument("--key-id", default="stoic-release-ed25519-v1")
    ap.add_argument("--timeout", default="10")
    a = ap.parse_args()
    token = a.token or (open(a.token_file).read().strip() if a.token_file else "")
    if not token:
        print("token required (--token or --token-file)"); return 2

    import requests
    from urllib.parse import urlparse
    import release_signing as rs

    url = a.url.rstrip("/")
    host = urlparse(url).hostname or ""
    env = {"APP_ENV": "production", "RELEASE_SIGNER": "external", "RELEASE_SIGNER_URL": url,
           "RELEASE_SIGNER_ALLOWED_HOSTS": host, "RELEASE_SIGNER_TOKEN": token,
           "RELEASE_SIGNER_KEY_ID": a.key_id, "RELEASE_PUBLIC_KEY_B64": a.public_key,
           "RELEASE_SIGNER_TIMEOUT": a.timeout}
    rec = {"url": url, "checks": {}}
    viol = rs.signer_config_violations(env)
    rec["checks"]["config"] = {"ok": not viol, "violations": viol}
    try:
        r = requests.get(f"{url}/healthz", timeout=10)
        rec["checks"]["healthz"] = {"ok": r.ok and r.json().get("status") == "ok"}
        r = requests.get(f"{url}/public-key", timeout=10)
        body = r.json()
        rec["checks"]["public_key"] = {"ok": r.ok and body.get("public_key_b64") == a.public_key
                                       and body.get("key_id") == a.key_id, "remote": body}
    except Exception as e:  # noqa: BLE001
        rec["checks"]["reachability"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    rec["checks"]["health_identity"] = rs.signer_health(env)
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        nonce = b"stoic-signer-probe:" + secrets.token_bytes(32)
        sig = rs._external_sign(nonce)
        rec["checks"]["sign_roundtrip"] = {
            "ok": rs.verify_hex(nonce, sig, a.public_key) and not rs.verify_hex(nonce + b"x", sig, a.public_key)}
    except Exception as e:  # noqa: BLE001
        rec["checks"]["sign_roundtrip"] = {"ok": False, "error": str(e)}
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    rec["result"] = "PASS" if all(c.get("ok") for c in rec["checks"].values()) else "FAIL"
    print(json.dumps(rec, indent=2, default=str))
    return 0 if rec["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
