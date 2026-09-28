#!/usr/bin/env python3
"""Post-redeploy live probe for stoicaibot.com (read-only, unauthenticated).

    python scripts/live_probe.py [--base https://www.stoicaibot.com] [--expect-sha <commit>]

PASS only when the live API serves the audited build: /api/health 200 with
build_sha (== --expect-sha when given), app_env=production, env_sig present;
/api/status carries the R15 `trading.attestation` block; /api/authority/decision
200; /api/authority/decision and /api/ledger/statements 401 (routes exist,
auth required — the old build answers 404). Exit 0 = PASS.
"""
import argparse
import json
import subprocess
import sys

import requests


def _get(base, path):
    try:
        r = requests.get(base + path, timeout=20)
        try:
            body = r.json()
        except ValueError:
            body = r.text[:200]
        return r.status_code, body
    except requests.RequestException as e:
        return 0, f"{type(e).__name__}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://www.stoicaibot.com")
    ap.add_argument("--expect-sha")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    expect = a.expect_sha
    if expect is None:
        try:
            expect = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        except Exception:  # noqa: BLE001
            expect = None
    checks = {}

    code, h = _get(base, "/api/health")
    hd = h if isinstance(h, dict) else {}
    sha = str(hd.get("build_sha") or "")
    checks["health"] = {
        "ok": code == 200 and hd.get("status") == "ok" and bool(sha)
              and hd.get("app_env") == "production" and bool(hd.get("env_sig"))
              and (not expect or expect.startswith(sha) or sha.startswith(expect[:12])),
        "http": code, "build_sha": sha or None, "app_env": hd.get("app_env"),
        "env_sig_present": bool(hd.get("env_sig")), "expected_sha": expect}

    code, s = _get(base, "/api/status")
    sd = s if isinstance(s, dict) else {}
    att = (sd.get("trading") or {}).get("attestation") if isinstance(sd.get("trading"), dict) else None
    checks["status_readiness"] = {"ok": code == 200 and isinstance(att, dict) and "attested" in att,
                                  "http": code, "attestation": att,
                                  "legacy_flat_status": code == 200 and "trading" not in sd}

    code, _ = _get(base, "/api/authority/decision")
    checks["authority_decision"] = {"ok": code == 401, "http": code}

    code, _ = _get(base, "/api/ledger/statements")
    checks["ledger_statements"] = {"ok": code == 401, "http": code}

    result = "PASS" if all(c["ok"] for c in checks.values()) else "FAIL"
    verdict = ("live serves the audited production build" if result == "PASS" else
               "live still serves the OLD build" if checks["authority_decision"]["http"] == 404 else
               "new build present but readiness/provenance checks failed")
    print(json.dumps({"base": base, "result": result, "verdict": verdict, "checks": checks}, indent=2))
    return 0 if result == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
