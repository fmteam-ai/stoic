"""Unit — scripts/release_attestation.py emit/verify content gate."""
import importlib.util
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SCRIPT = os.path.join(ROOT, "scripts", "release_attestation.py")
SHA = "a" * 40

JUNIT_OK = ('<testsuites><testsuite tests="600" failures="0" errors="0" '
            'skipped="3"/></testsuites>')
JUNIT_BAD = ('<testsuite tests="600" failures="2" errors="0" skipped="0"/>')
PIP_CLEAN = {"dependencies": [{"name": "anyio", "version": "4.14.2", "vulns": []}]}
PIP_VULN = {"dependencies": [{"name": "anyio", "version": "4.14.0",
                              "vulns": [{"id": "CVE-2026-63374"}]}]}
GRYPE_CLEAN = {"matches": [{"vulnerability": {"severity": "High",
                                              "fix": {"state": "fixed"}}}]}
GRYPE_CRIT = {"matches": [{"vulnerability": {"severity": "Critical",
                                             "fix": {"state": "fixed"}}}]}


def _mod():
    spec = importlib.util.spec_from_file_location("ra", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _emit(tmp, junit=JUNIT_OK, pip=PIP_CLEAN, grype=GRYPE_CLEAN, extra=()):
    j = tmp / "junit.xml"; j.write_text(junit)
    p = tmp / "pip.json"; p.write_text(json.dumps(pip))
    g = tmp / "grype.json"; g.write_text(json.dumps(grype))
    out = tmp / "att.json"
    r = subprocess.run([sys.executable, SCRIPT, "emit", "--sha", SHA,
                        "--tag", "v9.9.9", "--backend-digest", "ghcr.io/x@sha256:1",
                        "--junit", str(j), "--pip-audit", str(p), "--grype", str(g),
                        "--install-ready", "--ea-compiled", "--out", str(out),
                        *extra], capture_output=True, text=True)
    return r.returncode, out


def _verify(out, *args):
    r = subprocess.run([sys.executable, SCRIPT, "verify", "--file", str(out),
                        *args], capture_output=True, text=True)
    return r.returncode, r.stdout


def test_emit_approved_and_verify_ok(tmp_path):
    rc, out = _emit(tmp_path)
    assert rc == 0
    att = json.loads(out.read_text())
    assert att["promotion_decision"] == "APPROVED"
    assert att["tests"] == {"tests": 600, "failures": 0, "errors": 0,
                            "skipped": 3, "files": ["junit.xml"], "passed": 597}
    assert att["scans"]["pip_audit"]["vulnerable_packages"] == 0
    assert att["scans"]["grype_backend"]["fixable_critical"] == 0
    assert all(att["gates"].values())
    rc, msg = _verify(out, "--sha", SHA, "--tag", "v9.9.9",
                      "--backend-digest", "ghcr.io/x@sha256:1")
    assert rc == 0 and "VERIFY OK" in msg


def test_verify_rejects_sha_mismatch(tmp_path):
    _, out = _emit(tmp_path)
    rc, msg = _verify(out, "--sha", "b" * 40)
    assert rc == 2 and "commit" in msg


def test_failed_tests_reject(tmp_path):
    rc, out = _emit(tmp_path, junit=JUNIT_BAD)
    assert rc == 2
    assert json.loads(out.read_text())["promotion_decision"] == "REJECTED"
    assert _verify(out, "--sha", SHA)[0] == 2


def test_vulnerable_dependency_rejects(tmp_path):
    rc, out = _emit(tmp_path, pip=PIP_VULN)
    assert rc == 2 and _verify(out, "--sha", SHA)[0] == 2


def test_fixable_critical_image_vuln_rejects(tmp_path):
    rc, out = _emit(tmp_path, grype=GRYPE_CRIT)
    assert rc == 2 and _verify(out, "--sha", SHA)[0] == 2


def test_too_few_tests_rejects(tmp_path):
    rc, out = _emit(tmp_path, extra=("--min-tests", "1000"))
    assert rc == 2
    assert _verify(out, "--sha", SHA, "--min-tests", "1000")[0] == 2


def test_missing_scan_never_passes(tmp_path):
    m = _mod()
    assert m._pip_audit(None)["ran"] is False
    assert m._grype(None, None)["ran"] is False


def test_deploy_scripts_enforce_the_gate():
    upd = open(os.path.join(ROOT, "deploy", "update.sh")).read()
    inst = open(os.path.join(ROOT, "deploy", "install.sh")).read()
    lib = open(os.path.join(ROOT, "deploy", "lib.sh")).read()
    assert "verify_attestation || rollback" in upd
    assert "verify_attestation ||" in inst
    assert "cosign verify-blob" in lib and "--certificate-oidc-issuer" in lib
    rel = open(os.path.join(ROOT, ".github", "workflows", "release.yml")).read()
    assert "release_attestation.py emit" in rel
    assert "release-attestation.json.sig" in rel
