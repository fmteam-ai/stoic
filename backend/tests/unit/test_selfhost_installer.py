"""Self-host installer — signer sidecar wiring, pinned TLS, bootstrap script."""
import os
import subprocess

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(*parts):
    return open(os.path.join(ROOT, *parts)).read()


def test_compose_has_signer_sidecar_with_tls_and_file_secrets():
    d = yaml.safe_load(_read("docker-compose.yml"))
    s = d["services"]["signer"]
    assert s["environment"]["SIGNER_TOKEN_FILE"] == "/run/secrets/signer_token"
    assert s["environment"]["ED25519_SIGNING_KEY_B64_FILE"] == "/run/secrets/signer_ed25519_key"
    assert "--ssl-certfile" in s["command"] and "--ssl-keyfile" in s["command"]
    assert "ports" not in s                                     # internal network only
    env = d["services"]["backend"]["environment"]
    assert env["RELEASE_SIGNER_TOKEN_FILE"] == "/run/secrets/signer_token"
    assert env["RELEASE_SIGNER_CA_BUNDLE"] == "/run/secrets/signer_cert"
    assert env["ORDER_AUTH_SECRET_FILE"] != env["LEDGER_ANCHOR_KEY_FILE"]
    assert "signer_ed25519_key" not in d["services"]["backend"]["secrets"]   # API never holds the private key
    assert d["services"]["backend"]["depends_on"]["signer"]["condition"] == "service_healthy"
    for name in ("order_auth_secret", "ledger_anchor_key", "signer_token", "signer_ed25519_key",
                 "signer_cert", "signer_cert_key"):
        assert name in d["secrets"], name


def test_installer_configures_external_signer_and_prod_hardening():
    inst = _read("deploy", "install.sh")
    for needle in ("RELEASE_SIGNER external", "RELEASE_SIGNER_URL https://signer:9443",
                   "RELEASE_SIGNER_ALLOWED_HOSTS signer", "RELEASE_PUBLIC_KEY_B64 \"${SIGNER_PUB_B64}\"",
                   "RELEASE_SIGNER_DEFERRED false", "ADMIN_MFA_ENFORCED true", "TURNSTILE_EXPECTED_HOSTNAMES",
                   "secrets/order_auth_secret", "secrets/ledger_anchor_key", "openssl req -x509",
                   "/^ED25519_SIGNING_KEY_B64=/d", "/^STEP_UP_BYPASS_TOKEN=/d"):
        assert needle in inst, needle
    for script in ("deploy/install.sh", "deploy/bootstrap.sh"):
        subprocess.run(["bash", "-n", os.path.join(ROOT, script)], check=True)


def test_bootstrap_supports_rhel_and_debian_with_rollback_and_diagnostics():
    b = _read("deploy", "bootstrap.sh")
    assert b.index("0/5 system check") < b.index("1/5 prerequisites") < b.index("deploy/install.sh \"${MODE}\"")
    for needle in ("dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo",
                   "dnf -y -q remove podman", "firewall-cmd -q --permanent --add-service=https", "getenforce",
                   "dnf -y -q install python3.11", "alternatives --set python3",
                   "apt-get install -y -qq docker-ce", "ufw allow 443/tcp",
                   "git clone", "deploy/install.sh", "--production", "--dev", "--repo", "--skip-attestation",
                   "trap rollback ERR", "snapshot()", "rollback()", "stoic-rollback:", "secrets.tgz",
                   "deploy/doctor.sh --bundle", "deploy/doctor.sh --quiet", "releases.log",
                   # step 0 — read-only system check gates every change
                   "0/5 system check (read-only)", "--check-only", "--strict", "nothing was changed on this host",
                   "registry-1.docker.io", "NTPSynchronized", "is_cloudflare_ip", "all green — proceeding"):
        assert needle in b, needle
    for script in ("bootstrap.sh", "doctor.sh"):
        assert os.access(os.path.join(ROOT, "deploy", script), os.X_OK), script
        subprocess.run(["bash", "-n", os.path.join(ROOT, "deploy", script)], check=True)


def test_installer_generates_ed25519_with_openssl_and_labels_selinux():
    inst = _read("deploy", "install.sh")
    assert "openssl genpkey -algorithm ed25519" in inst and "tail -c 32 | base64 -w0" in inst
    assert "chcon -Rt container_file_t secrets" in inst
    assert "import cryptography" not in inst          # no Python packages needed on the host


def test_doctor_reports_and_redacts():
    d = _read("deploy", "doctor.sh")
    for needle in ("RELEASE_SIGNER=external", "ED25519_SIGNING_KEY_B64", "container_file_t", "/api/health/ready",
                   "release-readiness", "=<redacted>", "doctor: ${FAILS} FAIL"):
        assert needle in d, needle
    assert "cat secrets/" not in d.replace("cat secrets/metrics_token", "")   # only the metrics token is read, never exported
    assert "tar -czf" in d and "secrets_listing" in d


def test_signer_app_reads_file_secrets():
    app_src = _read("deploy", "signer", "app.py")
    assert '_secret("SIGNER_TOKEN")' in app_src and '_secret("ED25519_SIGNING_KEY_B64")' in app_src
    assert 'os.environ.get(f"{name}_FILE")' in app_src


def test_ca_bundle_pins_but_never_disables_verification():
    import sys
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    from release_signing import _tls_verify
    assert _tls_verify({}) is True
    assert _tls_verify({"RELEASE_SIGNER_CA_BUNDLE": "/run/secrets/signer_cert"}) == "/run/secrets/signer_cert"
    assert _tls_verify({"RELEASE_SIGNER_CA_BUNDLE": "  "}) is True
    src = _read("backend", "release_signing.py")
    assert "verify=False" not in src
