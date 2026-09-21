#!/usr/bin/env python3
"""Release attestation — emitted by CI, VERIFIED by deploy scripts.

stdlib only (runs on a bare server before any image is built).

  emit    build release-attestation.json from CI evidence
          (junit XMLs, pip-audit JSON, grype JSON, manifest, digests)
  fetch   download release-attestation.json{,.sig,.pem} for a tag from the
          GitHub release (GITHUB_TOKEN optional; required for private repos)
  verify  content gate: commit SHA matches, zero failed/errored tests,
          minimum test count, zero pip-audit vulns, zero fixable critical
          image vulns, gates all green. Signature verification is done by
          cosign in deploy/lib.sh (keyless, Sigstore) — this tool checks the
          CONTENT the signature covers.

Exit codes: 0 ok · 2 content gate failed · 3 fetch failed · 4 bad input.
"""
import argparse
import re
import hashlib
import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

RECORD = "release-attestation"
SCHEMA = 1
MIN_TESTS_DEFAULT = 500


def _junit_counts(paths: list) -> dict:
    tot = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "files": []}
    for p in paths:
        root = ET.parse(p).getroot()
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        for s in suites:
            for k in ("tests", "failures", "errors", "skipped"):
                tot[k] += int(s.get(k) or 0)
        tot["files"].append(os.path.basename(p))
    tot["passed"] = tot["tests"] - tot["failures"] - tot["errors"] - tot["skipped"]
    return tot


def _pip_audit(path: str | None) -> dict:
    if not path or not os.path.exists(path):
        return {"tool": "pip-audit", "ran": False, "vulnerable_packages": None}
    data = json.load(open(path))
    deps = data.get("dependencies", data if isinstance(data, list) else [])
    vuln = [d for d in deps if d.get("vulns")]
    return {"tool": "pip-audit", "ran": True, "packages_audited": len(deps),
            "vulnerable_packages": len(vuln),
            "findings": [{"name": d.get("name"), "version": d.get("version"),
                          "ids": [v.get("id") for v in d.get("vulns", [])]}
                         for d in vuln]}


def _grype(path: str | None, image: str | None) -> dict:
    if not path or not os.path.exists(path):
        return {"tool": "grype", "ran": False, "image": image,
                "fixable_critical": None}
    data = json.load(open(path))
    matches = data.get("matches", [])
    by_sev: dict = {}
    fixable_crit = 0
    for m in matches:
        v = m.get("vulnerability", {})
        sev = (v.get("severity") or "Unknown").lower()
        by_sev[sev] = by_sev.get(sev, 0) + 1
        if sev == "critical" and (v.get("fix") or {}).get("state") == "fixed":
            fixable_crit += 1
    return {"tool": "grype", "ran": True, "image": image,
            "matches_by_severity": by_sev, "fixable_critical": fixable_crit,
            "db_built": (data.get("descriptor") or {}).get("db", {}).get("built")}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def cmd_emit(a) -> int:
    manifest = json.load(open(a.manifest)) if a.manifest and os.path.exists(a.manifest) else {}
    att = {
        "record": RECORD, "schema": SCHEMA,
        "emitted_at": datetime.now(timezone.utc).isoformat(),
        "tag": a.tag, "commit": a.sha,
        "images": {"backend": a.backend_digest, "frontend": a.frontend_digest},
        "tests": _junit_counts(a.junit or []),
        "scans": {"pip_audit": _pip_audit(a.pip_audit),
                  "grype_backend": _grype(a.grype, a.backend_digest)},
        "gates": {
            "unit_and_suite_tests": None,      # filled below
            "dependency_audit": None,
            "image_scan": None,
            "clean_install_readiness": bool(a.install_ready),
            "ea_compile": bool(a.ea_compiled),
        },
        "artifacts": {"release_manifest_sha256": _sha256(a.manifest)
                      if a.manifest and os.path.exists(a.manifest) else None,
                      "ea_version": manifest.get("ea_version")},
        "provenance": {
            "builder": "github-actions",
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "signing": "cosign keyless sign-blob (Sigstore OIDC) over this file",
        },
        "commands": a.command or [],
        "policy": {"min_tests": a.min_tests, "max_failed_tests": 0,
                   "max_pip_audit_vulns": 0, "max_fixable_critical_image_vulns": 0},
    }
    t = att["tests"]
    att["gates"]["unit_and_suite_tests"] = (t["failures"] == 0 and t["errors"] == 0
                                            and t["tests"] >= a.min_tests)
    pa = att["scans"]["pip_audit"]
    att["gates"]["dependency_audit"] = bool(pa["ran"] and pa["vulnerable_packages"] == 0)
    gr = att["scans"]["grype_backend"]
    att["gates"]["image_scan"] = bool(gr["ran"] and gr["fixable_critical"] == 0)
    att["promotion_decision"] = ("APPROVED" if all(att["gates"].values())
                                 else "REJECTED")
    json.dump(att, open(a.out, "w"), indent=2, sort_keys=True)
    print(json.dumps(att, indent=2, sort_keys=True))
    return 0 if att["promotion_decision"] == "APPROVED" else 2


def _gh(url: str, accept: str) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept,
                                               "User-Agent": "stoic-deploy"})
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def cmd_fetch(a) -> int:
    api = f"https://api.github.com/repos/{a.repo}/releases/tags/{a.tag}"
    try:
        rel = json.loads(_gh(api, "application/vnd.github+json"))
    except Exception as e:  # noqa: BLE001
        print(f"fetch failed: {api}: {e}", file=sys.stderr)
        return 3
    assets = {x["name"]: x["url"] for x in rel.get("assets", [])}
    os.makedirs(a.dest, exist_ok=True)
    for name in ("release-attestation.json", "release-attestation.json.sig",
                 "release-attestation.json.pem"):
        if name not in assets:
            print(f"fetch failed: asset {name} missing on release {a.tag}",
                  file=sys.stderr)
            return 3
        try:
            data = _gh(assets[name], "application/octet-stream")
        except Exception as e:  # noqa: BLE001
            print(f"fetch failed: {name}: {e}", file=sys.stderr)
            return 3
        open(os.path.join(a.dest, name), "wb").write(data)
        print(f"fetched {name} ({len(data)} bytes)")
    return 0


def cmd_verify(a) -> int:
    try:
        att = json.load(open(a.file))
    except Exception as e:  # noqa: BLE001
        print(f"VERIFY FAIL: unreadable attestation: {e}")
        return 4
    problems = []
    if att.get("record") != RECORD:
        problems.append(f"record is {att.get('record')!r}, expected {RECORD!r}")
    if a.sha and att.get("commit") != a.sha:
        problems.append(f"commit {att.get('commit')} != deployed {a.sha}")
    if a.tag and att.get("tag") != a.tag:
        problems.append(f"tag {att.get('tag')} != {a.tag}")
    t = att.get("tests") or {}
    if t.get("failures", 1) or t.get("errors", 1):
        problems.append(f"tests failed={t.get('failures')} errors={t.get('errors')}")
    if (t.get("tests") or 0) < a.min_tests:
        problems.append(f"only {t.get('tests')} tests recorded (< {a.min_tests})")
    pa = (att.get("scans") or {}).get("pip_audit") or {}
    if not pa.get("ran") or pa.get("vulnerable_packages"):
        problems.append(f"pip-audit: ran={pa.get('ran')} vulnerable={pa.get('vulnerable_packages')}")
    gr = (att.get("scans") or {}).get("grype_backend") or {}
    if not gr.get("ran") or gr.get("fixable_critical"):
        problems.append(f"grype: ran={gr.get('ran')} fixable_critical={gr.get('fixable_critical')}")
    gates = att.get("gates") or {}
    bad = [k for k, v in gates.items() if v is not True]
    if bad:
        problems.append(f"gates not green: {', '.join(bad)}")
    if att.get("promotion_decision") != "APPROVED":
        problems.append(f"promotion_decision={att.get('promotion_decision')}")
    images = att.get("images") or {}
    for name, got in (("backend", a.backend_digest), ("frontend", a.frontend_digest)):
        want = images.get(name)
        if got and want and want != got:
            problems.append(f"{name} image digest {got} != attested {want}")
    if a.require_images:
        for name in ("backend", "frontend"):
            if not _digest_pinned(images.get(name)):
                problems.append(f"{name} image is not a digest-pinned reference: {images.get(name)!r}")
    if problems:
        print("VERIFY FAIL:")
        for p in problems:
            print(f"  - {p}")
        return 2
    print(f"VERIFY OK: {att['tag']} @ {att['commit']} · "
          f"{t.get('passed')}/{t.get('tests')} tests passed · "
          f"pip-audit clean · 0 fixable critical image vulns · "
          f"decision {att['promotion_decision']}")
    return 0


def _digest_pinned(ref) -> bool:
    return bool(ref) and bool(re.fullmatch(r"[a-z0-9./_-]+@sha256:[0-9a-f]{64}", str(ref)))


def cmd_images(a) -> int:
    """Print the attested image refs as shell assignments (eval'd by deploy/lib.sh)."""
    try:
        att = json.load(open(a.file))
    except Exception as e:  # noqa: BLE001
        print(f"echo 'IMAGES FAIL: unreadable attestation: {e}'; false")
        return 4
    images = att.get("images") or {}
    for name in ("backend", "frontend"):
        if not _digest_pinned(images.get(name)):
            print(f"echo 'IMAGES FAIL: {name} image not digest-pinned in attestation'; false")
            return 2
    print(f"ATT_BACKEND_IMAGE={images['backend']}")
    print(f"ATT_FRONTEND_IMAGE={images['frontend']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("emit")
    e.add_argument("--sha", required=True)
    e.add_argument("--tag", required=True)
    e.add_argument("--backend-digest")
    e.add_argument("--frontend-digest")
    e.add_argument("--junit", action="append")
    e.add_argument("--pip-audit")
    e.add_argument("--grype")
    e.add_argument("--manifest")
    e.add_argument("--install-ready", action="store_true")
    e.add_argument("--ea-compiled", action="store_true")
    e.add_argument("--command", action="append")
    e.add_argument("--min-tests", type=int, default=MIN_TESTS_DEFAULT)
    e.add_argument("--out", default="release-attestation.json")
    f = sub.add_parser("fetch")
    f.add_argument("--repo", required=True, help="owner/name")
    f.add_argument("--tag", required=True)
    f.add_argument("--dest", default="release/attestation")
    v = sub.add_parser("verify")
    v.add_argument("--file", required=True)
    v.add_argument("--sha")
    v.add_argument("--tag")
    v.add_argument("--backend-digest")
    v.add_argument("--frontend-digest")
    v.add_argument("--require-images", action="store_true",
                   help="fail unless both images are digest-pinned (registry deploys)")
    v.add_argument("--min-tests", type=int, default=MIN_TESTS_DEFAULT)
    i = sub.add_parser("images", help="print attested image refs as shell assignments")
    i.add_argument("--file", required=True)
    a = ap.parse_args()
    return {"emit": cmd_emit, "fetch": cmd_fetch, "verify": cmd_verify,
            "images": cmd_images}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
