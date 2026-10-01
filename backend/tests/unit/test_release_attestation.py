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


def _hermetic_results(tmp, sha=None, result="PASS", steps=None, signed=True):
    """A scripts/verify_release.sh results file (HMAC-signed like the script does)."""
    import hashlib, hmac
    body = {"verification": "release", "build": sha or SHA, "at": "t",
            "steps": steps or {"backend_unit": "PASS", "frontend_build": "PASS"}, "result": result}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    if signed:
        body["signature"] = hmac.new(b"k" * 32, payload, hashlib.sha256).hexdigest()
        body["evidence_hash"] = hashlib.sha256(payload).hexdigest()
    h = tmp / "hermetic.json"; h.write_text(json.dumps(body))
    return h


def _rc_lock(tmp, sha=None):
    """CI-bound rc_lock (round 9 P1-06) matching the emitted digests."""
    d = {"git_commit": sha or SHA, "source_sha": sha or SHA, "authoritative": True,
         "images": {"backend": "ghcr.io/x/stoic-backend@sha256:" + "1" * 64,
                    "frontend": "ghcr.io/x/stoic-frontend@sha256:" + "2" * 64},
         "deployment_target": "production", "signer_key_id": "stoic-release-ed25519-v1",
         "test_manifest_sha256": "f" * 64, "model_manifest_sha256": "e" * 64}
    p = tmp / "rc_lock.json"; p.write_text(json.dumps(d))
    return p


def _canary(tmp, result="PASS"):
    p = tmp / "canary.json"; p.write_text(json.dumps({"result": result, "mode": "external", "key_id": "k"}))
    return p


def _emit(tmp, junit=JUNIT_OK, pip=PIP_CLEAN, grype=GRYPE_CLEAN, extra=(), hermetic=None,
          rc_lock=None, canary=None):
    extra = (*extra, "--rc-lock", str(rc_lock or _rc_lock(tmp)), "--signer-canary", str(canary or _canary(tmp)))
    j = tmp / "junit.xml"; j.write_text(junit)
    p = tmp / "pip.json"; p.write_text(json.dumps(pip))
    g = tmp / "grype.json"; g.write_text(json.dumps(grype))
    out = tmp / "att.json"
    h = hermetic if hermetic is not None else _hermetic_results(tmp)
    r = subprocess.run([sys.executable, SCRIPT, "emit", "--sha", SHA,
                        "--tag", "v9.9.9", "--backend-digest", "ghcr.io/x/stoic-backend@sha256:" + "1" * 64,
                        "--frontend-digest", "ghcr.io/x/stoic-frontend@sha256:" + "2" * 64,
                        "--junit", str(j), "--pip-audit", str(p), "--grype", str(g),
                        "--install-ready", "--ea-compiled", "--hermetic-verification", str(h), "--out", str(out),
                        *extra], capture_output=True, text=True)
    return r.returncode, out


def test_hermetic_verification_gate(tmp_path):
    """Release evidence must include a PASS, signed, commit-bound hermetic run."""
    rc, out = _emit(tmp_path)
    assert rc == 0 and json.load(open(out))["gates"]["hermetic_verification"] is True
    for bad in (_hermetic_results(tmp_path, result="FAIL", steps={"backend_unit": "FAIL"}),
                _hermetic_results(tmp_path, signed=False),
                _hermetic_results(tmp_path, sha="f" * 40),
                tmp_path / "missing.json"):
        rc, out = _emit(tmp_path, hermetic=bad)
        att = json.load(open(out))
        assert rc == 2 and att["gates"]["hermetic_verification"] is False and att["promotion_decision"] == "REJECTED"
        assert _verify(out)[0] != 0


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
                      "--backend-digest", "ghcr.io/x/stoic-backend@sha256:" + "1" * 64,
                      "--frontend-digest", "ghcr.io/x/stoic-frontend@sha256:" + "2" * 64,
                      "--require-images")
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
    assert "verify_attestation || gate_refused" in upd and "gate_refused() {" in upd
    assert "verify_attestation ||" in inst
    assert "cosign verify-blob" in lib and "--certificate-oidc-issuer" in lib
    rel = open(os.path.join(ROOT, ".github", "workflows", "release.yml")).read()
    assert "release_attestation.py emit" in rel
    assert "release-attestation.json.sig" in rel


# ── Registry image deploys (pull attested digests, no local rebuild) ─────────
def _images(out):
    r = subprocess.run([sys.executable, SCRIPT, "images", "--file", str(out)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout


def test_images_prints_shell_assignments_for_deploy_lib(tmp_path):
    _, out = _emit(tmp_path)
    rc, txt = _images(out)
    assert rc == 0
    assert "ATT_BACKEND_IMAGE=ghcr.io/x/stoic-backend@sha256:" + "1" * 64 in txt
    assert "ATT_FRONTEND_IMAGE=ghcr.io/x/stoic-frontend@sha256:" + "2" * 64 in txt


def test_frontend_digest_mismatch_rejects(tmp_path):
    _, out = _emit(tmp_path)
    rc, msg = _verify(out, "--sha", SHA, "--frontend-digest", "ghcr.io/x/stoic-frontend@sha256:" + "f" * 64)
    assert rc == 2 and "frontend image digest" in msg


def test_require_images_rejects_tag_or_missing_refs(tmp_path):
    _, out = _emit(tmp_path)
    att = json.loads(out.read_text())
    att["images"]["frontend"] = "ghcr.io/x/stoic-frontend:v9.9.9"   # tag, not digest
    out.write_text(json.dumps(att))
    rc, msg = _verify(out, "--sha", SHA, "--require-images")
    assert rc == 2 and "not a digest-pinned" in msg
    rc, txt = _images(out)
    assert rc == 2 and "false" in txt
    att["images"]["frontend"] = None
    out.write_text(json.dumps(att))
    assert _verify(out, "--sha", SHA, "--require-images")[0] == 2
    assert _verify(out, "--sha", SHA)[0] == 0   # build mode: digests optional


def test_registry_mode_wiring():
    lib = open(os.path.join(ROOT, "deploy", "lib.sh")).read()
    assert "pull_attested_images()" in lib and "provision_images()" in lib
    assert 'cosign verify "${d}"' in lib and "docker pull -q" in lib
    assert "--require-images" in lib
    # registry mode forces the attestation gate regardless of ATTESTATION_REQUIRED
    assert '[ "$(deploy_mode)" = "registry" ] && return 0' in lib
    for f in ("update.sh", "install.sh", "rollback.sh"):
        body = open(os.path.join(ROOT, "deploy", f)).read()
        assert "provision_images ||" in body, f
        assert "compose_up" in body, f
        assert "build_with_provenance ||" not in body, f"{f} must go through provision_images"
    reg = open(os.path.join(ROOT, "docker-compose.registry.yml")).read()
    assert "STOIC_BACKEND_IMAGE" in reg and "STOIC_FRONTEND_IMAGE" in reg
    assert "pull_policy: never" in reg
    rel = open(os.path.join(ROOT, ".github", "workflows", "release.yml")).read()
    assert "--require-images" in rel


def test_rc_lock_and_signer_canary_gates(tmp_path):
    """Round 9: promotion REJECTED unless the rc_lock is bound to this commit +
    digests and the external signer canary PASSED."""
    rc, out = _emit(tmp_path)
    g = json.load(open(out))["gates"]
    assert rc == 0 and g["rc_lock_bound"] is True and g["signer_canary"] is True
    rc, out = _emit(tmp_path, rc_lock=_rc_lock(tmp_path, sha="9" * 40))
    assert rc == 2 and json.load(open(out))["gates"]["rc_lock_bound"] is False
    rc, out = _emit(tmp_path, canary=_canary(tmp_path, "FAIL"))
    assert rc == 2 and json.load(open(out))["gates"]["signer_canary"] is False
    rc, out = _emit(tmp_path, canary=_canary(tmp_path, "SKIPPED"))
    assert rc == 2
