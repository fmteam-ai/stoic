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


def test_bootstrap_installs_docker_clones_and_delegates():
    b = _read("deploy", "bootstrap.sh")
    for needle in ("download.docker.com", "docker-compose-plugin", "git clone", "deploy/install.sh",
                   "--production", "--dev", "--repo", "ufw allow 443/tcp"):
        assert needle in b, needle
    assert os.access(os.path.join(ROOT, "deploy", "bootstrap.sh"), os.X_OK)


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
