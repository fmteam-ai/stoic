"""Self-host installer — signer sidecar wiring, pinned TLS, bootstrap script."""
import os
import shutil
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
    expected = "order_auth_secret ledger_anchor_key signer_token signer_ed25519_key signer_cert signer_cert_key mongo_keyfile"
    for name in expected.split():
        assert name in d["secrets"], name


def test_compose_mongo_is_replica_set_with_keyfile_and_self_initiating_healthcheck():
    # audit r17 P0-01: production fails closed without transactions → the stack must ship a replica set
    d = yaml.safe_load(_read("docker-compose.yml"))
    m = d["services"]["mongo"]
    assert m["command"] == ["sh", "/stoic-mongo-start.sh"]
    assert "mongo_keyfile" in m["secrets"]
    assert "./deploy/mongo-start.sh:/stoic-mongo-start.sh:ro" in m["volumes"]
    hc = " ".join(m["healthcheck"]["test"])
    assert "rs.status()" in hc and 'rs.initiate({_id: "rs0", members: [{_id: 0, host: "mongo:27017"}]})' in hc
    assert "isWritablePrimary ? 0 : 1" in hc                     # green only when PRIMARY
    assert "/run/secrets/mongo_root_password" in hc and "$MONGO_INITDB_ROOT_USERNAME" in hc
    assert "ports" not in m                                       # never published to the host
    start = _read("deploy", "mongo-start.sh")
    # the image drops to `mongodb` before reading the root password / init secret / keyFile: root-only host
    # secrets must be staged as mongodb-owned private copies (server run #4: "Permission denied" restart loop)
    assert "for f in mongo_keyfile mongo_root_password mongo_app_password; do" in start
    assert 'install -m 400 -o mongodb -g mongodb "/run/secrets/$f" "$S/$f"' in start
    assert 'export MONGO_INITDB_ROOT_PASSWORD_FILE="$S/mongo_root_password"' in start
    assert 'export MONGO_APP_PASSWORD_FILE="$S/mongo_app_password"' in start
    assert 'exec docker-entrypoint.sh mongod --bind_ip_all --replSet rs0 --keyFile "$S/mongo_keyfile" "$@"' in start
    init = _read("deploy", "mongo-init.js")
    assert 'process.env.MONGO_APP_PASSWORD_FILE || "/run/secrets/mongo_app_password"' in init
    assert os.access(os.path.join(ROOT, "deploy", "mongo-start.sh"), os.X_OK)
    # the forecast override keeps the wrapper (a plain mongod command would silently drop the replica set)
    f = yaml.safe_load(_read("docker-compose.forecast.yml"))
    assert f["services"]["mongo"]["command"][:2] == ["sh", "/stoic-mongo-start.sh"]
    inst = _read("deploy", "install.sh")
    assert "openssl rand -base64 756 | tr -d '\\n' > secrets/mongo_keyfile" in inst
    assert "authSource=%s&replicaSet=rs0" in inst and "printf '&replicaSet=rs0' >> secrets/mongo_url" in inst


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
                   "registry-1.docker.io", "NTPSynchronized", "is_cloudflare_ip", "all green — proceeding",
                   # behind-proxy mode: an existing Apache/nginx keeps 80/443 and reverse-proxies to loopback
                   '--behind-proxy) MODE="--behind-proxy"', "re-run with --behind-proxy ${DOMAIN}",
                   "will reverse-proxy to STOIC (behind-proxy mode)",
                   # MongoDB is a container: the check says so and flags a host mongod / broken mongodb-org repo
                   "mongodb: runs in Docker (mongo:7 container, 127.0.0.1 only) — no host install needed",
                   "mongodb: host mongod found", "/etc/yum.repos.d/mongodb-org-*.repo", "deploy/doctor.sh --db",
                   # AlmaLinux ships podman-docker: `docker` is a podman shim and must not pass as Docker Engine
                   "is_podman_shim() {", 'case "${v}" in *[Pp]odman*) return 0 ;; esac', "bootstrap.sh ${BOOTSTRAP_VERSION}",
                   "is the podman-docker shim", "dnf -y -q remove podman-docker podman buildah runc",
                   "if is_podman_shim || ! command -v docker >/dev/null || ! docker compose version"):
        assert needle in b, needle
    # curl | bash safety: the body is one brace group (parsed fully before execution) so a child that
    # drains stdin (docker compose exec during the snapshot dump) cannot truncate the streamed script
    body = b[b.index('BOOTSTRAP_VERSION="'):]
    assert body.split("\n", 4)[4].startswith("{") and b.rstrip().endswith("exit 0\n}")
    assert 'bash deploy/backup.sh backup </dev/null' in b
    piped = subprocess.run(["bash", "-s"], input="{\necho a\ncat >/dev/null\necho b\nexit 0\n}\n", capture_output=True, text=True)
    assert piped.stdout == "a\nb\n"
    # SIGPIPE (exit 141) hardening: no early-exiting reader downstream of a long writer under pipefail
    assert b.index("set +o pipefail") < b.index("for p in 80 443") < b.index("all green — proceeding") < b.index("set -o pipefail\n")
    import re
    assert not re.search(r"\|\s*awk[^\n|]*\bexit\b", b), "awk … exit in a pipeline can SIGPIPE the writer"
    assert "| head -1" not in b and "grep -q ." not in b
    for script in ("bootstrap.sh", "doctor.sh"):
        assert os.access(os.path.join(ROOT, "deploy", script), os.X_OK), script
        subprocess.run(["bash", "-n", os.path.join(ROOT, "deploy", script)], check=True)


def test_doctor_db_check_runs_inside_container_without_exposing_password():
    d = _read("deploy", "doctor.sh")
    assert "--db) DBONLY=1" in d and "db_check()" in d and 'doctor ${LABEL}: ${FAILS} FAIL' in d
    # the full report also includes the DB section
    assert d.index("db_check\n\nhdr \"endpoints\"") > d.index("hdr \"containers\"")
    for needle in ("server version:", "ping:", "app user auth + write/read round trip", "replica set:",
                   "data volume:", "no host mongod (expected: MongoDB is the mongo:7 container)",
                   "production requires transactions", "MongoDB must never be reachable from outside"):
        assert needle in d, needle
    # password is read from the container's own secret mount, never from host secrets/ and never passed as an argument
    assert 'cat /run/secrets/mongo_app_password' in d
    assert "cat secrets/mongo_app_password" not in d and "-p \"$(cat secrets" not in d
    assert "cat secrets/mongo_url" not in d and "cat secrets/mongo_root_password" not in d


def test_behind_proxy_mode_renders_apache_and_nginx_snippets(tmp_path):
    inst = _read("deploy", "install.sh")
    for needle in ("--behind-proxy)", 'elif [ "${MODE}" = "--behind-proxy" ]; then', "deploy/proxy/render.sh",
                   '[ "${MODE}" = "--production" ] || [ "${MODE}" = "--behind-proxy" ]; then\n  set_kv backend/.env APP_ENV production'):
        assert needle.replace("\\n", "\n") in inst, needle
    proj = tmp_path / "p"
    (proj / "deploy" / "proxy").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "deploy", "proxy", "render.sh"), proj / "deploy" / "proxy" / "render.sh")
    subprocess.run(["bash", "deploy/proxy/render.sh", "trade.example.com"], cwd=proj, check=True, capture_output=True)
    apache = (proj / "deploy" / "proxy" / "apache-trade.example.com.conf").read_text()
    nginx = (proj / "deploy" / "proxy" / "nginx-trade.example.com.conf").read_text()
    assert "ProxyPass        /api http://127.0.0.1:8001/api" in apache and 'X-Forwarded-Proto "https"' in apache
    assert "ws://127.0.0.1:8001/api/$1" in apache and "userdata/ssl/2_4" in apache        # cPanel include path
    assert "proxy_pass         http://127.0.0.1:8001;" in nginx and 'Connection        "upgrade"' in nginx


def test_installer_generates_ed25519_with_openssl_and_labels_selinux():
    inst = _read("deploy", "install.sh")
    assert "openssl genpkey -algorithm ed25519" in inst and "tail -c 32 | base64 -w0" in inst
    assert "chcon -Rt container_file_t secrets" in inst
    assert "import cryptography" not in inst          # no Python packages needed on the host


def test_doctor_reports_and_redacts():
    d = _read("deploy", "doctor.sh")
    for needle in ("RELEASE_SIGNER=external", "ED25519_SIGNING_KEY_B64", "container_file_t", "/api/health/ready",
                   "release-readiness", "=<redacted>", "doctor: ${FAILS} FAIL",
                   # r24 SEC-001: rendered compose config is redacted and the bundle is leak-scanned before shipping
                   'key + ": <redacted>', "diagnostics bundle NOT written", 'chmod 600 "${OUT}.tar.gz"'):
        assert needle in d, needle
    b = _read("deploy", "bootstrap.sh")
    for needle in ("is not a valid hostname", "is not a valid e-mail address", "--telegram must be", "is not a valid git URL"):
        assert needle in b, needle
    assert "is not a valid hostname" in _read("deploy", "install.sh")
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


def test_doctor_backup_now_dumps_verifies_and_fails_closed():
    d = _read("deploy", "doctor.sh")
    assert "--backup-now) DBONLY=1; BACKUP_NOW=1" in d and "backup_now()" in d
    for needle in ("bash deploy/backup.sh backup", 'bash deploy/backup.sh verify "${OUT}"', "RESTORE VERIFICATION PASSED",
                   "refusing to write an unencrypted archive", "skipping backup: the database check above reported",
                   'verified backup: $(readlink -f "${OUT}")', "doctor ${LABEL}: ${FAILS} FAIL"):
        assert needle in d, needle
    assert "deploy/doctor.sh --db --backup-now" in _read("deploy", "bootstrap.sh")


def test_cloudflare_mode_uses_origin_ca_cert_and_can_pin_edges(tmp_path):
    b = _read("deploy", "bootstrap.sh")
    for needle in ('--cloudflare) MODE="--production"; CLOUDFLARE=1', "--origin-cert) ORIGIN_CERT=", "--cf-only) CF_ONLY=1",
                   "--cloudflare needs --origin-cert <cert.pem> --origin-key <key.pem>",
                   "only apply to --cloudflare <domain>", "orange cloud — expected in --cloudflare mode",
                   "origin certificate does not cover ${DOMAIN}", "the key does not match the certificate",
                   "install -m 600 \"${ORIGIN_CERT}\" secrets/origin_cert.pem", "install -m 600 \"${ORIGIN_KEY}\" secrets/origin_key.pem"):
        assert needle in b, needle
    inst = _read("deploy", "install.sh")
    for needle in ("--cloudflare) CLOUDFLARE=1", "--cf-only) CF_ONLY=1", "--cloudflare needs --production <domain>",
                   "docker-compose.yml:docker-compose.tls.yml:docker-compose.cloudflare.yml", "set_kv .env CLOUDFLARE_MODE true",
                   'bash deploy/cloudflare/render.sh "${DOMAIN}"', "does not match secrets/origin_cert.pem",
                   "*loud[fF]lare*", '--resolve "${DOMAIN}:443:127.0.0.1"', "--cf-only but a direct connection was served",
                   "SSL/TLS mode must be 'Full (strict)'"):
        assert needle in inst, needle
    ov = yaml.safe_load(_read("docker-compose.cloudflare.yml"))
    c = ov["services"]["caddy"]
    assert "./deploy/cloudflare/Caddyfile:/etc/caddy/Caddyfile:ro" in c["volumes"]
    assert c["secrets"] == ["origin_cert", "origin_key"]
    assert ov["secrets"]["origin_cert"]["file"] == "./secrets/origin_cert.pem"
    assert ov["secrets"]["origin_key"]["file"] == "./secrets/origin_key.pem"
    assert "deploy/cloudflare/Caddyfile" in _read(".gitignore")
    ips = [l for l in _read("deploy", "cloudflare", "ips.txt").split() if l]
    assert "104.16.0.0/13" in ips and "2606:4700::/32" in ips and len(ips) >= 20
    # render offline (curl stubbed to fail) → bundled list, both variants
    work = tmp_path / "repo"
    (work / "deploy" / "cloudflare").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "deploy", "cloudflare", "render.sh"), work / "deploy" / "cloudflare" / "render.sh")
    shutil.copy(os.path.join(ROOT, "deploy", "cloudflare", "ips.txt"), work / "deploy" / "cloudflare" / "ips.txt")
    stub = tmp_path / "bin"; stub.mkdir(); (stub / "curl").write_text("#!/bin/sh\nexit 7\n"); (stub / "curl").chmod(0o755)
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"}
    subprocess.run(["bash", "deploy/cloudflare/render.sh", "trade.example.com"], cwd=work, env=env, check=True, capture_output=True)
    cf = (work / "deploy" / "cloudflare" / "Caddyfile").read_text()
    assert "trade.example.com, www.trade.example.com {" in cf
    assert "tls /run/secrets/origin_cert /run/secrets/origin_key" in cf
    assert "trusted_proxies static 173.245.48.0/20" in cf or "trusted_proxies static " in cf and "173.245.48.0/20" in cf
    assert "abort @notcf" not in cf
    subprocess.run(["bash", "deploy/cloudflare/render.sh", "trade.example.com", "--cf-only"], cwd=work, env=env, check=True, capture_output=True)
    cf = (work / "deploy" / "cloudflare" / "Caddyfile").read_text()
    assert "@notcf not remote_ip " in cf and "abort @notcf" in cf and "2606:4700::/32" in cf
    bad = subprocess.run(["bash", "deploy/cloudflare/render.sh", "bad host;rm"], cwd=work, env=env, capture_output=True)
    assert bad.returncode == 1


def test_installer_locks_itself_after_success_and_refuses_reruns(tmp_path):
    b = _read("deploy", "bootstrap.sh")
    for needle in ("--unlock) UNLOCK=1", 'LOCK_FILE="${LOCK_DIR}/.stoic-installed"', "the installer is LOCKED", "exit 3",
                   "lock_install() {", 'chattr +i "$f"', 'lock_install "${TARGET}"', '[ "${HAD_LOCK}" = 1 ] && lock_install "${TARGET}"',
                   "== STOIC is installed — installer LOCKED ==", "export STOIC_INSTALL_UNLOCK=1"):
        assert needle in b, needle
    assert b.index('lock_install "${TARGET}"\nbash deploy/install_report.sh') > 0      # report records the lock
    inst = _read("deploy", "install.sh")
    assert '[ -f .stoic-installed ] && [ "${STOIC_INSTALL_UNLOCK:-0}" != 1 ] && [ "${UNLOCK_ARG}" != 1 ]' in inst
    assert "installer_locked" in _read("deploy", "install_report.sh")
    assert "installer: LOCKED since" in _read("deploy", "doctor.sh")
    # behaviour: locked → exit 3 for both scripts; --check-only and --unlock proceed; install.sh honours the env override
    proj = tmp_path / "p"; (proj / "deploy").mkdir(parents=True)
    for f in ("bootstrap.sh", "install.sh"):
        shutil.copy(os.path.join(ROOT, "deploy", f), proj / "deploy" / f)
    (proj / "docker-compose.yml").write_text("services: {}\n")
    (proj / ".stoic-installed").write_text("installed_at=2026-06-01T10:00:00Z\nmode=behind-proxy\n")
    stub = tmp_path / "bin"; stub.mkdir()
    (stub / "id").write_text("#!/bin/sh\necho 0\n"); (stub / "id").chmod(0o755)      # pretend root
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"}
    env.pop("STOIC_INSTALL_UNLOCK", None)
    r = subprocess.run(["bash", "deploy/bootstrap.sh", "--behind-proxy", "trade.example.com"], cwd=proj, env=env, capture_output=True, text=True)
    assert r.returncode == 3 and "installer is LOCKED" in r.stdout and "deploy/update.sh" in r.stdout
    r = subprocess.run(["bash", "deploy/install.sh", "--behind-proxy", "trade.example.com"], cwd=proj, env=env, capture_output=True, text=True)
    assert r.returncode == 3 and "installer is LOCKED" in r.stdout
    (stub / "docker").write_text("#!/bin/sh\nexit 1\n"); (stub / "docker").chmod(0o755)   # no docker → fails AFTER the lock gate
    r = subprocess.run(["bash", "deploy/install.sh", "--behind-proxy", "trade.example.com"], cwd=proj,
                       env={**env, "STOIC_INSTALL_UNLOCK": "1"}, capture_output=True, text=True)
    assert r.returncode == 1 and "LOCKED" not in r.stdout and "docker" in r.stdout
    r = subprocess.run(["bash", "deploy/bootstrap.sh", "--behind-proxy", "trade.example.com", "--check-only"],
                       cwd=proj, env=env, capture_output=True, text=True, timeout=90)
    assert "LOCKED" not in r.stdout and "0/5 system check" in r.stdout and (proj / ".stoic-installed").exists()   # read-only run never touches the lock
    # --unlock removes the lock only AFTER the system check is green (a red check leaves the host locked)
    assert b.index("all green — proceeding") < b.index("removing install lock") < b.index("1/5 prerequisites")


def test_compose_up_clears_containers_marked_for_removal():
    lib = _read("deploy", "lib.sh")
    assert "docker compose ps -aq --status removing --status dead" in lib
    assert lib.index("removing stale containers") < lib.index("docker compose up -d --remove-orphans")
    assert "systemctl restart docker" in lib
    # last resort for mounts leaked into another namespace (cPanel CageFS/LVE): metadata removal with dockerd stopped, zombie IDs only
    assert 'rm -rf "/var/lib/docker/containers/$z"' in lib and "systemctl stop docker docker.socket" in lib
    assert lib.index("docker inspect -f '{{.GraphDriver.Data.MergedDir}}' \"$z\"") < lib.index("systemctl stop docker docker.socket")


def test_cpanel_wiring_script_is_safe_and_autowired():
    c = _read("deploy", "proxy", "cpanel.sh")
    for needle in ("/scripts/whoowns", "/scripts/rebuildhttpdconf", "/scripts/ensure_vhost_includes", "/scripts/restartsrv_httpd",
                   "userdata/ssl/2_4/${CPUSER}/${D}", "userdata/std/2_4/${CPUSER}/${D}", "ProxyPass /.well-known !",
                   "RewriteCond %{REQUEST_URI} !^/\\.well-known/", "ea-apache24-mod_proxy_wstunnel",
                   'httpd -t 2>&1 | grep -q "Syntax OK"', '--resolve "${D}:443:127.0.0.1"', "public_html is left untouched"):
        assert needle in c, needle
    assert '[[ "${CPUSER}" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]' in c                    # user name validated before path use
    assert os.access(os.path.join(ROOT, "deploy", "proxy", "cpanel.sh"), os.X_OK)
    subprocess.run(["bash", "-n", os.path.join(ROOT, "deploy", "proxy", "cpanel.sh")], check=True)
    b = _read("deploy", "bootstrap.sh")
    assert '[ "${MODE}" = "--behind-proxy" ] && [ -x /scripts/rebuildhttpdconf ]' in b
    assert 'bash deploy/proxy/cpanel.sh "${DOMAIN}"' in b and "cPanel wiring failed (non-fatal" in b
    bad = subprocess.run(["bash", os.path.join(ROOT, "deploy", "proxy", "cpanel.sh"), "bad;host"], capture_output=True, text=True)
    assert bad.returncode == 1 and "usage" in bad.stdout


def test_app_images_stage_root_only_secrets_then_drop_privileges():
    # server run #8: API/workers run as `stoic` and could not read root-only host secrets (Permission denied)
    df = _read("Dockerfile.backend")
    assert "USER stoic" not in df                                   # privileges are dropped by the entrypoint instead
    assert 'COPY --chmod=755 deploy/app-entrypoint.sh /usr/local/bin/app-entrypoint.sh' in df
    assert 'ENTRYPOINT ["/usr/local/bin/app-entrypoint.sh"]' in df and "command -v setpriv" in df
    ep = _read("deploy", "app-entrypoint.sh")
    assert 'install -m 400 -o stoic -g stoic "$f" "$S/$(basename "$f")"' in ep
    assert "grep -E '^[A-Za-z_][A-Za-z0-9_]*=/run/secrets/[^/]+$'" in ep       # only exact secret paths are re-pointed
    assert 'exec setpriv --reuid=stoic --regid=stoic --init-groups -- "$@"' in ep
    assert os.access(os.path.join(ROOT, "deploy", "app-entrypoint.sh"), os.X_OK)
    subprocess.run(["sh", "-n", os.path.join(ROOT, "deploy", "app-entrypoint.sh")], check=True)
