#!/usr/bin/env python3
"""Publish a dependency-scan result to the platform (daily report D1/D2).

    pip-audit -r reqs.txt --no-deps -f json -o /tmp/pip.json || true
    python3 scripts/publish_dependency_audit.py --kind python   --input /tmp/pip.json
    (cd frontend && npm audit --omit=dev --json > /tmp/npm.json || true)
    python3 scripts/publish_dependency_audit.py --kind frontend --input /tmp/npm.json

Needs STOIC_PUBLISH_URL (public origin, e.g. https://stoicaibot.com) and STOIC_METRICS_TOKEN in the environment;
with either missing it prints what it WOULD publish and exits 0 (forks / PRs without secrets never fail on this).
Normalised shape: {package, version, id, score, fix}. Never prints the token."""
import argparse
import json
import os
import subprocess
import sys
import urllib.request

SEV_SCORE = {"critical": 9.5, "high": 7.5, "moderate": 5.0, "medium": 5.0, "low": 2.0, "info": 0.5}


def normalise_pip_audit(doc: dict) -> list:
    out = []
    for dep in doc.get("dependencies") or []:
        for v in dep.get("vulns") or []:
            out.append({"package": dep.get("name"), "version": dep.get("version"), "id": v.get("id"),
                        "score": 7.5, "fix": ", ".join(v.get("fix_versions") or []) or None})
    return out


def normalise_npm_audit(doc: dict) -> list:
    out = []
    for name, v in (doc.get("vulnerabilities") or {}).items():
        via = [x for x in (v.get("via") or []) if isinstance(x, dict)]
        ids = [str(x.get("source") or x.get("url") or x.get("title") or "") for x in via] or [v.get("severity") or "advisory"]
        fix = v.get("fixAvailable")
        fix_s = (f"{fix.get('name')}@{fix.get('version')}" if isinstance(fix, dict) else ("available" if fix else None))
        out.append({"package": name, "version": ",".join(v.get("range", "").split("||"))[:60] if v.get("range") else "",
                    "id": ids[0][:80], "score": SEV_SCORE.get(str(v.get("severity") or "").lower(), 0.0), "fix": fix_s})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["python", "frontend"], required=True)
    ap.add_argument("--input", required=True, help="pip-audit / npm audit JSON (missing or empty file = clean run)")
    ap.add_argument("--source", default=os.environ.get("GITHUB_WORKFLOW") or "ci")
    ap.add_argument("--require", action="store_true", help="fail (exit 1) when the publish secrets are unset — the scheduled daily job")
    a = ap.parse_args()
    doc = {}
    if os.path.exists(a.input) and os.path.getsize(a.input) > 0:
        try:
            doc = json.load(open(a.input, encoding="utf-8"))
        except ValueError:
            doc = {}
    vulns = normalise_pip_audit(doc) if a.kind == "python" else normalise_npm_audit(doc)
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        commit = os.environ.get("GITHUB_SHA", "")
    payload = {"kind": a.kind, "vulnerabilities": vulns, "source": a.source, "commit": commit,
               "tool": "pip-audit" if a.kind == "python" else "npm audit"}
    url, token = os.environ.get("STOIC_PUBLISH_URL", "").rstrip("/"), os.environ.get("STOIC_METRICS_TOKEN", "")
    print(f"dependency audit [{a.kind}]: {len(vulns)} finding(s) from {a.input}")
    if not url or not token:
        if a.require:
            # v1.60.43 — the DAILY job is the only producer of D1/D2: skipping silently left the reports on
            # "no result stored" for weeks. Fail so the missing repository secrets are noticed.
            print("ERROR: STOIC_PUBLISH_URL / STOIC_METRICS_TOKEN not set — the daily dependency-audit MUST publish D1/D2 "
                  "(Settings → Secrets: STOIC_PUBLISH_URL = public origin, STOIC_METRICS_TOKEN = METRICS_TOKEN of backend/.env)")
            return 1
        print("STOIC_PUBLISH_URL / STOIC_METRICS_TOKEN not set — not published (dry run)")
        return 0
    req = urllib.request.Request(f"{url}/api/ops/dependency-audit", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"published: HTTP {r.status} {r.read()[:200].decode(errors='ignore')}")
    except Exception as e:  # noqa: BLE001
        print(f"publish failed: {type(e).__name__}: {str(e)[:200]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
