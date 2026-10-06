"""main101 review — N101-1 (lock adoption in update.sh), N101-2 (release gate exempts attested DEMO),
N101-3 (restore trap under set -u), N101-5 (purpose-required signing, runtime manifest purpose, no
single-token fallback, separate key ids/pins), N101-6 (backup passphrase provisioning), N101-7."""
import asyncio
import base64
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import textwrap
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _signer_env():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as s
    k = Ed25519PrivateKey.generate()
    priv = base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode()
    pub = base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode()
    return {"RELEASE_SIGNER": "local", "APP_ENV": "preview", "ED25519_SIGNING_KEY_B64": priv,
            "RELEASE_PUBLIC_KEY_B64": pub, "RELEASE_SIGNER_KEY_ID": "stoic-release-ed25519-v1"}, priv, pub


# ── N101-2 ──────────────────────────────────────────────────────────────────────────────────────
class _Db:
    pass


def _demo_account():
    return {"_id": "a1", "mode": "live", "broker_environment": "DEMO",
            "environment_attestation": {"environment": "DEMO", "status": "attested"}}


def test_n101_2_release_gate_exempts_attested_demo_but_keeps_live_close_only():
    import trading_authority as ta
    bad = {"ok": False, "failures": ["rc_lock is not authoritative (developer snapshot)"], "lock_commit": None}
    with patch.dict(os.environ, {"APP_ENV": "production"}), patch("release_gate.evaluate", lambda **k: bad):
        with patch("broker_env.attested_environment", lambda a: "DEMO"):
            d = _run(ta.release_gate_domain(_Db(), _demo_account()))
            assert d["level"] == "FULL" and "DEMO" in d["reason"]
        with patch("broker_env.attested_environment", lambda a: "LIVE"):
            d = _run(ta.release_gate_domain(_Db(), {"_id": "a2", "mode": "live"}))
            assert d["level"] == "CLOSE_ONLY" and d["code"] == "RELEASE_NOT_AUTHORITATIVE"
        # platform scope (no account) is evaluated per account (N102-1); paper is never gated
        assert "per account" in _run(ta.release_gate_domain(_Db()))["reason"]
        assert _run(ta.release_gate_domain(_Db()))["level"] == "FULL"
        assert _run(ta.release_gate_domain(_Db(), {"_id": "p", "mode": "paper"}))["level"] == "FULL"
    assert ta.DOMAIN_SCOPE["release_gate"] == "account_bound"


def test_n101_2_admin_release_gate_reports_per_account_verdict():
    src = _read("backend/routes/admin_routes.py")
    assert '"accounts": accounts' in src and "attested_environment(acc)" in src
    assert "blocked — release not authoritative" in src
    ui = _read("frontend/src/components/AcceptanceBundleCard.jsx")
    assert 'data-testid="release-gate-accounts"' in ui and "release-gate-account-" in ui


# ── N101-3 / N101-7 — backup.sh ───────────────────────────────────────────────────────────────
def _bash_functions(names):
    """Extract top-level function bodies (and the cleanup globals) from deploy/backup.sh."""
    src = _read("deploy/backup.sh")
    out = ["_RESTORE_PLAIN=\"\"", "_SECRETS_TMP=\"\""]
    for n in names:
        m = re.search(rf"^{re.escape(n)}\(\) \{{\n.*?^\}}", src, re.S | re.M)
        assert m, n
        out.append(m.group(0))
    return "\n".join(out)


@pytest.mark.skipif(not shutil.which("openssl") or not shutil.which("bash"), reason="openssl/bash required")
def test_n101_3_restore_secrets_exits_zero_under_set_u_and_cleans_tmp(tmp_path):
    work = tmp_path / "w"; work.mkdir()
    (work / "secrets").mkdir(); (work / "secrets" / "jwt_secret").write_text("abc\n")
    pw = tmp_path / "pass"; pw.write_text("pw\n")
    (work / "backups").mkdir()
    dump = work / "backups" / "stoic-mongo-20260101-000000.archive.gz"; dump.write_bytes(b"x")
    enc = work / "backups" / "stoic-secrets-20260101-000000.tar.gz.enc"
    subprocess.run(f"cd {work} && tar -czf - secrets | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass file:{pw} -out {enc}",
                   shell=True, check=True)
    shutil.rmtree(work / "secrets")   # restore into an ABSENT secrets/
    script = textwrap.dedent(f"""
        set -euo pipefail
        cd {work}
        export BACKUP_PASSPHRASE_FILE={pw}
        {_bash_functions(["_encrypt_args", "_restore_cleanup", "_restore_secrets"])}
        _restore_secrets "{dump}"
        echo "AFTER_RESTORE rc=$?"
    """)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr          # the old RETURN+EXIT trap exited 1 here ("tmp: unbound variable")
    assert "unbound variable" not in r.stderr
    assert (work / "secrets" / "jwt_secret").read_text() == "abc\n"
    assert not [p for p in os.listdir("/tmp") if p.startswith("stoic-secrets-") and os.path.isdir(os.path.join("/tmp", p))
                and os.stat(os.path.join("/tmp", p)).st_mtime > os.stat(str(pw)).st_mtime - 1]


def test_n101_3_restore_uses_one_exit_trap_with_globals():
    src = _read("deploy/backup.sh")
    assert "trap 'rm -rf \"${tmp}\"' RETURN EXIT" not in src
    assert "trap _restore_cleanup EXIT" in src and "_SECRETS_TMP=" in src and "_RESTORE_PLAIN=" in src
    assert "local stamp sec tmp f" not in src


@pytest.mark.skipif(not shutil.which("bash"), reason="bash required")
def test_n101_7_env_reader_handles_missing_trailing_newline_and_quotes(tmp_path):
    src = _read("deploy/backup.sh")
    block = re.search(r"^if \[ -f \.env \]; then\n.*?^fi\n", src, re.S | re.M).group(0)
    (tmp_path / ".env").write_bytes(b'BACKUP_OFFSITE="true"\nBACKUP_PASSPHRASE_FILE=\'/root/.p\'')   # no trailing newline
    r = subprocess.run(["bash", "-c", f"set -euo pipefail; cd {tmp_path}\n{block}\necho \"$BACKUP_OFFSITE|$BACKUP_PASSPHRASE_FILE\""],
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "true|/root/.p", r.stdout + r.stderr


# ── N101-6 — passphrase provisioning ─────────────────────────────────────────────────────────────
@pytest.mark.skipif(not shutil.which("openssl") or not shutil.which("bash"), reason="openssl/bash required")
def test_n101_6_ensure_backup_passphrase_creates_file_outside_secrets_and_records_it(tmp_path):
    work = tmp_path / "w"; work.mkdir(); (work / "secrets").mkdir(); (work / ".env").write_text("APP_ENV=production\n")
    home = tmp_path / "home"; home.mkdir()
    lib = _read("deploy/lib.sh")
    fn = re.search(r"^set_kv\(\) \{.*?^\}", lib, re.S | re.M).group(0) + "\n" + \
         re.search(r"^ensure_backup_passphrase\(\) \{.*?^\}", lib, re.S | re.M).group(0)
    r = subprocess.run(["bash", "-c", f"set -euo pipefail; cd {work}; export HOME={home}\n{fn}\nensure_backup_passphrase; ensure_backup_passphrase; echo DONE"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "DONE" in r.stdout, r.stdout + r.stderr
    f = home / ".stoic-backup-pass"
    assert f.exists() and len(f.read_text().strip()) >= 40 and oct(f.stat().st_mode)[-3:] == "600"
    env = (work / ".env").read_text()
    assert env.count("BACKUP_PASSPHRASE_FILE=") == 1 and str(f) in env
    # refuses a passphrase inside ./secrets
    r2 = subprocess.run(["bash", "-c", f"set -euo pipefail; cd {work}\n{fn}\nBACKUP_PASSPHRASE_FILE={work}/secrets/p ensure_backup_passphrase"],
                        capture_output=True, text=True)
    assert r2.returncode != 0 and "refusing" in r2.stdout + r2.stderr


def test_n101_6_install_and_update_provision_the_passphrase():
    assert "ensure_backup_passphrase" in _read("deploy/install.sh")
    up = _read("deploy/update.sh")
    assert up.index("ensure_backup_passphrase") < up.index("deploy/backup.sh backup")


# ── N101-1 — update.sh adopts the authoritative lock from the signed release assets ─────────────
def test_n101_1_update_adopts_signed_release_lock_before_the_strict_provenance_gate():
    up = _read("deploy/update.sh"); lib = _read("deploy/lib.sh")
    assert up.index("verify_attestation || gate_refused") < up.index("adopt_release_lock || gate_refused") < up.index("verify_release_provenance || gate_refused")
    fn = re.search(r"^adopt_release_lock\(\) \{.*?^\}", lib, re.S | re.M).group(0)
    assert "cosign verify-blob" in fn and "SHA256SUMS" in fn and "rc_lock.json" in fn
    assert "release/rc_lock.json" in fn and "backend/BUILD_SHA" in fn and 'lock.get("authoritative") is True' in fn
    assert "deploy/releases/rc_lock-${GIT_SHA}.json" in fn
    # tracked files restored before every checkout; the previous lock re-adopted on refuse/rollback
    assert up.count("restore_tracked_release_files") >= 3 and up.count('restore_adopted_lock "${PREV}"') == 2
    assert "release/rc_lock.json" in re.search(r"^restore_tracked_release_files\(\) \{.*?^\}", lib, re.S | re.M).group(0)
    fetch = _read("scripts/release_attestation.py")
    for a in ('"rc_lock.json"', '"SHA256SUMS"', '"SHA256SUMS.sig"', '"SHA256SUMS.pem"'):
        assert a in fetch
    assert "deploy/releases/rc_lock-*.json" in _read(".gitignore")


def test_n101_7_deploy_production_waits_for_a_successful_release_run():
    wf = _read(".github/workflows/deploy-production.yml")
    assert "workflow_run:" in wf and 'workflows: ["Release"]' in wf
    assert "github.event.workflow_run.conclusion == 'success'" in wf
    assert "push:\n    tags" not in wf


# ── N101-5 — signer separation completed ─────────────────────────────────────────────────────────
def test_n101_5_sign_hex_requires_a_purpose_and_runtime_manifests_use_their_own_purpose():
    import inspect
    import release_signing as rs
    assert inspect.signature(rs.sign_hex).parameters["purpose"].default is inspect.Parameter.empty
    assert inspect.signature(rs.verify_hex).parameters["purpose"].default is inspect.Parameter.empty
    assert "artifact-manifest" in rs.API_PURPOSES and "policy-migration" in rs.RELEASE_PURPOSES
    for f in ("backend/vps_pathb.py", "backend/release_channels.py"):
        assert 'purpose="artifact-manifest"' in _read(f) and "sign_hex(body)" not in _read(f)
    assert 'purpose="artifact-manifest"' in _read("backend/runtime_validation.py")
    assert 'purpose="ea-release"' in _read("backend/ea_capabilities.py")
    env, _, _ = _signer_env()
    with patch.dict(os.environ, env):
        with pytest.raises(TypeError):
            rs.sign_hex(b"x")                       # purpose is mandatory
        sig = rs.sign_hex(b"m", purpose="artifact-manifest")
        assert rs.verify_hex(b"m", sig, purpose="artifact-manifest")
        assert not rs.verify_hex(b"m", sig, purpose="ea-release")


def test_n101_5_signer_sidecar_has_no_single_token_fallback_and_configurable_key_id():
    _, priv, pub = _signer_env()
    sidecar_env = {"SIGNER_TOKEN": "rel-token", "SIGNER_KEY_ID": "stoic-bundle-ed25519-v1", "SIGNER_ROLE": "release"}
    sidecar_env["ED25519_SIGNING_KEY" + "_B64"] = priv      # built at runtime (gitleaks false positive on the literal, main102)
    with patch.dict(os.environ, sidecar_env, clear=False):
        os.environ.pop("SIGNER_TOKEN_BUNDLE", None); os.environ.pop("SIGNER_TOKEN_BUNDLE_FILE", None)
        spec = importlib.util.spec_from_file_location("signer_app_n101", os.path.join(ROOT, "deploy", "signer", "app.py"))
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        from fastapi.testclient import TestClient
        c = TestClient(mod.app)
        assert mod.KEY_ID == "stoic-bundle-ed25519-v1"
        h = c.get("/health", headers={"Authorization": "Bearer rel-token"}).json()
        assert h["release_only"] is True and h["key_id"] == "stoic-bundle-ed25519-v1"
        body = {"key_id": mod.KEY_ID, "data_hex": b"rec".hex()}
        # release token: release purposes OK, runtime purposes REFUSED even without a bundle token
        assert c.post("/sign", json={**body, "purpose": "ea-release"}, headers={"Authorization": "Bearer rel-token"}).status_code == 200
        assert c.post("/sign", json={**body, "purpose": "acceptance-bundle"}, headers={"Authorization": "Bearer rel-token"}).status_code == 403
        assert c.post("/sign", json={**body, "purpose": "artifact-manifest"}, headers={"Authorization": "Bearer rel-token"}).status_code == 403
        import release_signing as rs
        assert mod.PURPOSES == rs.PURPOSES


def test_n101_5_separate_key_ids_and_pins_for_release_and_bundle_keys():
    import release_signing as rs
    env = {"RELEASE_SIGNER_KEY_ID": "stoic-release-ed25519-v1", "RELEASE_PUBLIC_KEY_B64": "R" * 44,
           "BUNDLE_SIGNER_KEY_ID": "stoic-bundle-ed25519-v1", "BUNDLE_PUBLIC_KEY_B64": "B" * 44}
    assert rs.key_id(env, "ea-release") == "stoic-release-ed25519-v1" and rs.key_id(env, "model-manifest") == "stoic-release-ed25519-v1"
    assert rs.key_id(env, "acceptance-bundle") == "stoic-bundle-ed25519-v1" and rs.key_id(env, "artifact-manifest") == "stoic-bundle-ed25519-v1"
    assert rs._pinned_pub(env, "ea-release") == "R" * 44 and rs._pinned_pub(env, "audit-anchor") == "B" * 44
    single = {"RELEASE_SIGNER_KEY_ID": "k", "RELEASE_PUBLIC_KEY_B64": "R" * 44}     # single-key install falls back
    assert rs.key_id(single, "acceptance-bundle") == "k" and rs._pinned_pub(single, "acceptance-bundle") == "R" * 44
    with patch.dict(os.environ, env):
        assert rs.key_id_accepted("stoic-release-ed25519-v1") and not rs.key_id_accepted("stoic-bundle-ed25519-v1")
        assert rs.key_id_accepted("stoic-bundle-ed25519-v1", purpose="acceptance-bundle")
    # an external signer answering with the wrong key id for the purpose is refused
    ext = {"APP_ENV": "preview", "RELEASE_SIGNER": "external", "RELEASE_SIGNER_URL": "https://signer:9443",
           "RELEASE_SIGNER_ALLOWED_HOSTS": "signer", "RELEASE_SIGNER_BUNDLE_TOKEN": "bun", "RELEASE_PUBLIC_KEY_B64": "R" * 44,
           "RELEASE_SIGNER_KEY_ID": "stoic-release-ed25519-v1",
           "BUNDLE_PUBLIC_KEY_B64": "B" * 44, "BUNDLE_SIGNER_KEY_ID": "stoic-bundle-ed25519-v1"}
    assert rs.signer_config_violations({**ext, "RELEASE_PUBLIC_KEY_B64": base64.b64encode(b"\0" * 32).decode(),
                                        "BUNDLE_PUBLIC_KEY_B64": base64.b64encode(b"\1" * 32).decode()}) == []
    assert any("RELEASE_SIGNER_BUNDLE_TOKEN" in v for v in rs.signer_config_violations({**ext, "RELEASE_SIGNER_BUNDLE_TOKEN": ""}))


def test_n101_5_api_never_holds_the_release_token():
    import yaml
    c = yaml.safe_load(_read("docker-compose.yml"))
    be = c["services"]["backend"]["environment"]
    assert "RELEASE_SIGNER_TOKEN" not in be and "RELEASE_SIGNER_TOKEN_FILE" not in be
    assert be["RELEASE_SIGNER_BUNDLE_TOKEN_FILE"] == "/run/secrets/signer_token_bundle"
    assert c["services"]["signer"]["environment"]["SIGNER_KEY_ID"].startswith("${SIGNER_KEY_ID:-")
    lib = _read("deploy/lib.sh")
    fn = re.search(r"^ensure_bundle_key_pins\(\) \{.*?^\}", lib, re.S | re.M).group(0)
    assert "sed -i '/^RELEASE_SIGNER_TOKEN=/d' backend/.env" in fn and "BUNDLE_PUBLIC_KEY_B64" in fn
    assert "ensure_bundle_key_pins" in _read("deploy/update.sh")
    inst = _read("deploy/install.sh")
    assert "BUNDLE_SIGNER_KEY_ID stoic-bundle-ed25519-v1" in inst and "SIGNER_KEY_ID stoic-bundle-ed25519-v1" in inst
    pf = _read("backend/deploy_preflight.py")
    assert "release_signer_token_scope" in pf
    canary = _read("scripts/signer_canary.py")
    assert 'purpose=purpose' in canary and 'purpose = "ea-release"' in canary


def test_n101_7_aria_pressed_moved_to_a_real_button_and_stale_health_message_fixed():
    ui = _read("frontend/src/components/LandingTestimonials.jsx")
    region = re.search(r'<div className="tst-marquee".*?>', ui, re.S).group(0)
    assert "aria-pressed" not in region and 'role="region"' in region
    assert 'data-testid="testimonials-pause-button"' in ui and "aria-pressed={paused}" in ui
    srv = _read("backend/server.py")
    assert '"ea_signed_record": bool(_signed_release_record())' in srv
    assert 'not os.environ.get("EA_RELEASE_SHA256")' not in srv
