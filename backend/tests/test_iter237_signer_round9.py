from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-237 — audit round 9 P1-01 (complete signer configuration validator +
canary), P1-06 (RC lock bound to the release commit) and P2-02 (no bytecode
in release archives).
"""
import base64
import json
import os
import subprocess
import sys

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(_BACKEND_DIR)
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_K = Ed25519PrivateKey.generate()
PRIV_B64 = base64.b64encode(_K.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                             serialization.NoEncryption())).decode()
PUB_B64 = base64.b64encode(_K.public_key().public_bytes(serialization.Encoding.Raw,
                                                        serialization.PublicFormat.Raw)).decode()

EXTERNAL_OK = {
    "RELEASE_SIGNER": "external",
    "RELEASE_SIGNER_URL": "https://signer.internal.example",
    "RELEASE_SIGNER_ALLOWED_HOSTS": "signer.internal.example",
    "RELEASE_SIGNER_TOKEN": "ref:kms/release-signer",
    "RELEASE_SIGNER_KEY_ID": "stoic-release-ed25519-v1",
    "RELEASE_PUBLIC_KEY_B64": PUB_B64,
    "RELEASE_SIGNER_TIMEOUT": "10",
}


def _env(monkeypatch, d):
    for k in ("RELEASE_SIGNER", "RELEASE_SIGNER_URL", "RELEASE_SIGNER_ALLOWED_HOSTS", "RELEASE_SIGNER_TOKEN",
              "RELEASE_SIGNER_KEY_ID", "RELEASE_PUBLIC_KEY_B64", "RELEASE_SIGNER_TIMEOUT",
              "ED25519_SIGNING_KEY_B64", "RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD", "APP_ENV",
              "RELEASE_SIGNER_DEFERRED", "PRODUCTION_RETIRED_SECRETS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in d.items():
        monkeypatch.setenv(k, v)


class TestSignerConfigValidator:
    def test_production_external_with_private_key_fails(self):
        from release_signing import signer_config_violations as v
        out = v({"APP_ENV": "production", **EXTERNAL_OK, "ED25519_SIGNING_KEY_B64": PRIV_B64})
        assert any("forbids ED25519_SIGNING_KEY_B64" in x for x in out)

    def test_production_missing_each_required_field_fails(self):
        from release_signing import signer_config_violations as v
        for k in ("RELEASE_SIGNER_URL", "RELEASE_SIGNER_TOKEN", "RELEASE_PUBLIC_KEY_B64",
                  "RELEASE_SIGNER_KEY_ID", "RELEASE_SIGNER_ALLOWED_HOSTS"):
            env = {"APP_ENV": "production", **EXTERNAL_OK}
            del env[k]
            assert v(env), k
        assert v({"APP_ENV": "production", **EXTERNAL_OK, "RELEASE_SIGNER_URL": "http://signer.internal.example"})
        assert v({"APP_ENV": "production", **EXTERNAL_OK, "RELEASE_SIGNER_URL": "https://evil.example"})
        assert v({"APP_ENV": "production", **EXTERNAL_OK, "RELEASE_SIGNER_TIMEOUT": "900"})
        assert v({"APP_ENV": "production", **EXTERNAL_OK, "RELEASE_PUBLIC_KEY_B64": "not-a-key"})
        assert v({"APP_ENV": "production", **EXTERNAL_OK}) == []

    def test_production_local_forbidden(self):
        from release_signing import signer_config_violations as v
        out = v({"APP_ENV": "production", "RELEASE_SIGNER": "local", "ED25519_SIGNING_KEY_B64": PRIV_B64})
        assert out and "forbids RELEASE_SIGNER=local" in out[0]

    def test_non_production_local_requires_matching_keypair(self):
        from release_signing import signer_config_violations as v
        assert v({"RELEASE_SIGNER": "local", "ED25519_SIGNING_KEY_B64": PRIV_B64}) == []
        assert v({"RELEASE_SIGNER": "local", "ED25519_SIGNING_KEY_B64": PRIV_B64, "RELEASE_PUBLIC_KEY_B64": PUB_B64}) == []
        other = base64.b64encode(b"\x01" * 32).decode()
        assert any("does not match" in x for x in
                   v({"RELEASE_SIGNER": "local", "ED25519_SIGNING_KEY_B64": PRIV_B64, "RELEASE_PUBLIC_KEY_B64": other}))
        assert v({"RELEASE_SIGNER": "local"})

    def test_boot_and_preflight_share_the_validator(self, monkeypatch):
        src = open(os.path.join(_BACKEND_DIR, "server.py")).read()
        assert "signer_config_violations" in src
        assert 'requires ED25519_SIGNING_KEY_B64 for' not in src
        from deploy_preflight import run_preflight
        _env(monkeypatch, {"APP_ENV": "production", **EXTERNAL_OK, "ED25519_SIGNING_KEY_B64": PRIV_B64})
        by = {c["id"]: c for c in run_preflight()["checks"]}
        assert by["release_signer"]["status"] == "fail" and by["ed25519"]["status"] == "fail"
        _env(monkeypatch, {"APP_ENV": "production", **EXTERNAL_OK})
        by = {c["id"]: c for c in run_preflight()["checks"]}
        assert by["release_signer"]["status"] == "pass" and by["ed25519"]["status"] == "pass"


class TestExternalSignerFailClosed:
    def _fake(self, monkeypatch, body=None, exc=None, status=200, raw=None):
        import requests

        class _R:
            status_code = status

            def raise_for_status(self):
                if status >= 400:
                    raise requests.HTTPError(f"{status}")

            def json(self):
                if raw is not None:
                    raise ValueError("not json")
                return body
        seen = {}

        def _post(url, json=None, headers=None, timeout=None, verify=True):
            seen.update(url=url, timeout=timeout, headers=headers, body=json)
            if exc:
                raise exc
            return _R()
        monkeypatch.setattr(requests, "post", _post)
        return seen

    def test_valid_external_signature_verifies_against_pinned_key(self, monkeypatch):
        import release_signing as rs
        _env(monkeypatch, {"APP_ENV": "production", **EXTERNAL_OK})
        data = b"round-9"
        seen = self._fake(monkeypatch, {"signature_hex": _K.sign(data).hex(), "key_id": EXTERNAL_OK["RELEASE_SIGNER_KEY_ID"]})
        sig = rs.sign_hex(data)
        assert rs.verify_hex(data, sig, PUB_B64)
        assert seen["url"] == "https://signer.internal.example/sign" and seen["timeout"] == 10.0

    def test_wrong_key_malformed_wrong_keyid_and_timeout_fail_closed(self, monkeypatch):
        import requests
        import release_signing as rs
        _env(monkeypatch, {"APP_ENV": "production", **EXTERNAL_OK})
        data = b"round-9"
        other = Ed25519PrivateKey.generate()
        self._fake(monkeypatch, {"signature_hex": other.sign(data).hex()})
        with pytest.raises(RuntimeError, match="pinned public key"):
            rs.sign_hex(data)
        self._fake(monkeypatch, raw="garbage")
        with pytest.raises(RuntimeError, match="malformed"):
            rs.sign_hex(data)
        self._fake(monkeypatch, {"signature_hex": _K.sign(data).hex(), "key_id": "some-other-key"})
        with pytest.raises(RuntimeError, match="key_id"):
            rs.sign_hex(data)
        self._fake(monkeypatch, exc=requests.ConnectTimeout("slow"))
        with pytest.raises(RuntimeError, match="unavailable"):
            rs.sign_hex(data)
        self._fake(monkeypatch, {"signature_hex": _K.sign(data).hex()}, status=503)
        with pytest.raises(RuntimeError, match="unavailable"):
            rs.sign_hex(data)

    def test_health_check_is_non_signing_and_checks_identity(self, monkeypatch):
        import requests
        import release_signing as rs
        _env(monkeypatch, {"APP_ENV": "production", **EXTERNAL_OK})
        calls = []

        class _R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": True, "key_id": EXTERNAL_OK["RELEASE_SIGNER_KEY_ID"], "public_key_b64": PUB_B64}
        monkeypatch.setattr(requests, "get", lambda url, headers=None, timeout=None, verify=True: calls.append(url) or _R())
        monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("health must not sign"))
        h = rs.signer_health(os.environ)
        assert h["ok"] and h["identity_matches"] and calls == ["https://signer.internal.example/health"]

        class _Bad(_R):
            def json(self):
                return {"ok": True, "key_id": EXTERNAL_OK["RELEASE_SIGNER_KEY_ID"], "public_key_b64": base64.b64encode(b"\x02" * 32).decode()}
        monkeypatch.setattr(requests, "get", lambda url, headers=None, timeout=None, verify=True: _Bad())
        assert rs.signer_health(os.environ)["ok"] is False

    def test_canary_script_pass_and_fail(self, monkeypatch, tmp_path):
        import requests
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import signer_canary
        _env(monkeypatch, {"APP_ENV": "production", **EXTERNAL_OK})

        class _H:
            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": True, "key_id": EXTERNAL_OK["RELEASE_SIGNER_KEY_ID"], "public_key_b64": PUB_B64}

        class _S:
            def __init__(self, data):
                self.d = data

            def raise_for_status(self):
                pass

            def json(self):
                return {"signature_hex": _K.sign(self.d).hex(), "key_id": EXTERNAL_OK["RELEASE_SIGNER_KEY_ID"]}
        monkeypatch.setattr(requests, "get", lambda *a, **k: _H())
        monkeypatch.setattr(requests, "post", lambda url, json=None, headers=None, timeout=None, verify=True: _S(bytes.fromhex(json["data_hex"])))
        rec = signer_canary.run(require_external=True)
        assert rec["result"] == "PASS" and rec["tamper_rejected"]
        _env(monkeypatch, {"RELEASE_SIGNER": "local", "ED25519_SIGNING_KEY_B64": PRIV_B64})
        assert signer_canary.run(require_external=True)["result"] == "FAIL"
        assert signer_canary.run(require_external=False)["result"] == "SKIPPED"


class TestRcLockBinding:
    def test_ci_lock_is_bound_and_attestation_gates_on_it(self, tmp_path):
        lock = tmp_path / "rc_lock.json"
        sha = "a" * 40
        be, fe = "ghcr.io/o/stoic-backend@sha256:" + "b" * 64, "ghcr.io/o/stoic-frontend@sha256:" + "c" * 64
        subprocess.check_call([sys.executable, os.path.join(ROOT, "scripts", "freeze_rc_lock.py"),
                               "--commit", sha, "--backend-digest", be, "--frontend-digest", fe,
                               "--target", "production", "--out", str(lock)], stdout=subprocess.DEVNULL)
        d = json.load(open(lock))
        assert d["authoritative"] is True and d["git_commit"] == sha and d["source_sha"] == sha
        assert d["images"] == {"backend": be, "frontend": fe} and d["signer_key_id"] and d["test_manifest_sha256"]
        # --check with a different release commit fails
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "freeze_rc_lock.py"), "--check",
                            "--commit", "f" * 40, "--out", str(lock)], capture_output=True, text=True)
        assert r.returncode == 1 and "git_commit" in r.stdout
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import release_attestation as ra
        ok = ra._rc_lock(str(lock), sha, be, fe)
        assert ok["bound"] is True
        assert ra._rc_lock(str(lock), "d" * 40, be, fe)["bound"] is False
        assert ra._rc_lock(str(lock), sha, "ghcr.io/o/stoic-backend@sha256:" + "e" * 64, fe)["bound"] is False
        assert ra._rc_lock(None, sha, be, fe)["bound"] is False

    def test_developer_snapshot_is_not_authoritative(self):
        d = json.load(open(os.path.join(ROOT, "release", "rc_lock.json")))
        assert d.get("authoritative") is False

    def test_runtime_rc_lock_check_rejects_snapshot_in_production(self):
        from release_truth import rc_lock_check
        out = rc_lock_check(production=True)
        assert out["present"] is True and out["ok"] is False and out["authoritative"] is False
        assert rc_lock_check(production=False)["ok"] is True

    def test_release_workflow_binds_lock_runs_canary_and_blocks_debris(self):
        wf = open(os.path.join(ROOT, ".github", "workflows", "release.yml")).read()
        assert 'freeze_rc_lock.py --commit "${COMMIT_SHA}"' in wf
        assert "--rc-lock rc_lock.json --signer-canary evidence/signer-canary.json" in wf
        assert "signer_canary.py --require-external" in wf
        assert "__pycache__" in wf and "RELEASE BLOCKED: build debris" in wf
        ga = open(os.path.join(ROOT, ".gitattributes")).read()
        assert "__pycache__/ export-ignore" in ga and "*.pyc export-ignore" in ga
        tracked = subprocess.check_output(["git", "-C", ROOT, "ls-files"], text=True)
        assert not any(l.endswith(".pyc") or "__pycache__" in l for l in tracked.splitlines())
