#!/usr/bin/env bash
# STOIC zero-touch server bootstrap — ONE command on a fresh host installs the
# whole project (Docker, source, secrets, signer sidecar, API + 6 workers,
# Mongo, frontend, TLS ingress) and verifies it. Supported hosts:
#   RHEL family : AlmaLinux 8/9, Rocky, CentOS Stream, RHEL   (dnf, firewalld, SELinux)
#   Debian family: Ubuntu 20.04+, Debian 11+                   (apt, ufw)
#
#   curl -fsSL https://raw.githubusercontent.com/<you>/<repo>/main/deploy/bootstrap.sh \
#     | sudo bash -s -- --production trade.example.com --repo https://github.com/<you>/<repo>.git
#   sudo bash deploy/bootstrap.sh --production trade.example.com     # from a checkout
#   sudo bash deploy/bootstrap.sh --dev                              # loopback only
#   sudo bash deploy/bootstrap.sh --behind-proxy trade.example.com   # Apache/cPanel/nginx already owns 80/443
#   sudo bash deploy/bootstrap.sh --cloudflare trade.example.com --origin-cert cert.pem --origin-key key.pem
#                                                                    # Cloudflare proxied (orange) with an Origin CA cert
#
# Options: --repo <git url> · --ref <tag|sha> · --target <dir> (default /opt/stoic)
#          --skip-attestation  build from your own checkout without a CI attestation record
#          --with-forecast · --registry (passed through to deploy/install.sh)
#          --origin-cert <pem> --origin-key <pem>  Cloudflare Origin CA certificate (--cloudflare)
#          --cf-only           origin answers ONLY to Cloudflare edge IPs (--cloudflare)
#          --no-rollback       keep the failed state for inspection
#          --check-only        run the system check (step 0) and exit — changes nothing
#          --unlock            re-run on a host that is already installed (the installer LOCKS
#                              itself after a successful install — upgrades go via deploy/update.sh)
#          --strict            treat system-check WARN as FAIL
#          --report-email <addr>  where to e-mail the signed install report (needs RESEND_API_KEY)
#          --telegram <bot_token>:<chat_id>  health alerts + install report to Telegram
#
# Safety: every run snapshots the previous state (git ref, .env files, secrets,
# image tags, DB dump when the stack is running). If ANY step fails the trap
# restores the snapshot, restarts the previous release and writes a diagnostics
# bundle (deploy/doctor.sh) — the host is never left half-installed.
set -euo pipefail
BOOTSTRAP_VERSION="r293"   # printed in the system-check header so a stale download is obvious
# The whole body is one brace group: bash must parse it completely before running a
# single command, so under `curl … | bash -s --` no child (docker compose exec, ssh,
# mongodump …) can swallow the rest of the script from the shared stdin.
{

MODE=""; DOMAIN=""; REPO="${STOIC_REPO_URL:-}"; REF="${STOIC_REF:-}"; TARGET="${STOIC_HOME:-/opt/stoic}"
SKIP_ATTEST=0; NO_ROLLBACK=0; EXTRA=(); REPORT_EMAIL=""; TELEGRAM=""; CHECK_ONLY=0; STRICT=0
CLOUDFLARE=0; CF_ONLY=0; ORIGIN_CERT=""; ORIGIN_KEY=""; UNLOCK=0; HAD_LOCK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --production) MODE="--production"; DOMAIN="${2:-}"; shift 2 ;;
    --behind-proxy) MODE="--behind-proxy"; DOMAIN="${2:-}"; shift 2 ;;
    --cloudflare) MODE="--production"; CLOUDFLARE=1; DOMAIN="${2:-}"; shift 2 ;;
    --origin-cert) ORIGIN_CERT="$2"; shift 2 ;;
    --origin-key) ORIGIN_KEY="$2"; shift 2 ;;
    --cf-only) CF_ONLY=1; shift ;;
    --dev) MODE="--dev"; shift ;;
    --repo) REPO="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --target) TARGET="$2"; shift 2 ;;
    --skip-attestation) SKIP_ATTEST=1; shift ;;
    --no-rollback) NO_ROLLBACK=1; shift ;;
    --check-only) CHECK_ONLY=1; shift ;;
    --unlock) UNLOCK=1; shift ;;
    --strict) STRICT=1; shift ;;
    --report-email) REPORT_EMAIL="$2"; shift 2 ;;
    --telegram) TELEGRAM="$2"; shift 2 ;;
    --with-forecast|--registry) EXTRA+=("$1"); shift ;;
    -h|--help) sed -n 2,22p "$0"; exit 0 ;;
    *) echo "unknown argument: $1"; exit 1 ;;
  esac
done
[ -n "${MODE}" ] || { echo "ERROR: choose --production <domain>, --behind-proxy <domain>, --cloudflare <domain> or --dev"; exit 1; }
[ "${MODE}" = "--dev" ] || [ -n "${DOMAIN}" ] || { echo "ERROR: ${MODE} needs a domain"; exit 1; }
if [ "${CLOUDFLARE}" = 1 ]; then
  [ -n "${ORIGIN_CERT}" ] && [ -n "${ORIGIN_KEY}" ] || { echo "ERROR: --cloudflare needs --origin-cert <cert.pem> --origin-key <key.pem> (Cloudflare dashboard → SSL/TLS → Origin Server → Create Certificate)"; exit 1; }
  [ -s "${ORIGIN_CERT}" ] || { echo "ERROR: ${ORIGIN_CERT} not found or empty"; exit 1; }
  [ -s "${ORIGIN_KEY}" ] || { echo "ERROR: ${ORIGIN_KEY} not found or empty"; exit 1; }
  ORIGIN_CERT="$(readlink -f "${ORIGIN_CERT}")"; ORIGIN_KEY="$(readlink -f "${ORIGIN_KEY}")"
  EXTRA+=(--cloudflare); [ "${CF_ONLY}" = 1 ] && EXTRA+=(--cf-only)
elif [ "${CF_ONLY}" = 1 ] || [ -n "${ORIGIN_CERT}${ORIGIN_KEY}" ]; then
  echo "ERROR: --cf-only / --origin-cert / --origin-key only apply to --cloudflare <domain>"; exit 1
fi
PUBLIC=0; [ "${MODE}" = "--dev" ] || PUBLIC=1
[ "$(id -u)" = 0 ] || { echo "ERROR: run as root (sudo)"; exit 1; }
# operator inputs are interpolated into sed/JSON/vhost files — accept only sane shapes
if [ -n "${DOMAIN}" ] && ! [[ "${DOMAIN}" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+$ ]]; then
  echo "ERROR: '${DOMAIN}' is not a valid hostname"; exit 1; fi
if [ -n "${REPORT_EMAIL}" ] && ! [[ "${REPORT_EMAIL}" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
  echo "ERROR: '${REPORT_EMAIL}' is not a valid e-mail address"; exit 1; fi
if [ -n "${TELEGRAM}" ] && ! [[ "${TELEGRAM}" =~ ^[0-9]+:[A-Za-z0-9_-]+:-?[0-9]+$ ]]; then
  echo "ERROR: --telegram must be <bot_token>:<chat_id> (e.g. 123456:ABC-def_ghi:-1001234567890)"; exit 1; fi
if [ -n "${REPO}" ] && ! [[ "${REPO}" =~ ^(https://|git@|ssh://)[A-Za-z0-9._@:/~+-]+$ ]]; then
  echo "ERROR: '${REPO}' is not a valid git URL"; exit 1; fi

# ------------------------------------------------------------------ install lock
# A successful install writes <project>/.stoic-installed (immutable where the
# filesystem allows). While it exists the installer refuses to run — a re-run
# rebuilds and restarts the whole stack, which is never an accident you want.
# Upgrades: deploy/update.sh. Deliberate reinstall: --unlock.
HERE="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd || true)"
LOCK_DIR="${TARGET}"; [ -f "${HERE}/deploy/install.sh" ] && [ -f "${HERE}/docker-compose.yml" ] && [ -z "${REPO}" ] && LOCK_DIR="${HERE}"
LOCK_FILE="${LOCK_DIR}/.stoic-installed"
if [ -f "${LOCK_FILE}" ] && [ "${CHECK_ONLY}" != 1 ]; then
  HAD_LOCK=1
  if [ "${UNLOCK}" != 1 ]; then
    echo "!! STOIC is already installed here — the installer is LOCKED."
    sed 's/^/   /' "${LOCK_FILE}"
    echo "   upgrades:   sudo bash ${LOCK_DIR}/deploy/update.sh [tag|sha]   (backup → rebuild → verify → auto-rollback)"
    echo "   diagnostics: sudo bash ${LOCK_DIR}/deploy/doctor.sh"
    echo "   reinstall on purpose: re-run with --unlock"
    exit 3
  fi
fi
lock_install() {   # $1 = project dir
  local f="$1/.stoic-installed"
  chattr -i "$f" 2>/dev/null || true; rm -f "$f"
  { echo "installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"; echo "mode=${MODE#--}"; echo "domain=${DOMAIN:-}"
    echo "commit=$(git -C "$1" rev-parse HEAD 2>/dev/null || echo unknown)"; echo "host=$(hostname -f 2>/dev/null || hostname)"
    echo "by=deploy/bootstrap.sh"; echo "unlock=sudo bash $1/deploy/bootstrap.sh ... --unlock"; } > "$f"
  chmod 444 "$f"
  if chattr +i "$f" 2>/dev/null; then echo "-- installer LOCKED: $f (immutable — chattr -i to remove by hand)"
  else echo "-- installer LOCKED: $f"; fi
}
export STOIC_INSTALL_UNLOCK=1   # bootstrap owns the lock; deploy/install.sh honours the same lock when run by hand

TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG="/var/log/stoic-bootstrap-${TS}.log"
exec > >(tee -a "${LOG}") 2>&1
log() { printf '\n== %s ==\n' "$*"; }
STEP="start"
SNAP=""

# ------------------------------------------------------------------ rollback
snapshot() {                       # called once the target dir is known
  SNAP="${TARGET}/.bootstrap-snapshots/${TS}"
  mkdir -p "${SNAP}"
  ( cd "${TARGET}" 2>/dev/null || exit 0
    git rev-parse HEAD > "${SNAP}/git_ref" 2>/dev/null || true
    for f in .env backend/.env; do [ -f "$f" ] && cp -a "$f" "${SNAP}/$(echo "$f" | tr / _)"; done
    [ -d secrets ] && tar -czf "${SNAP}/secrets.tgz" secrets
    if [ -n "$(docker compose ps -q 2>/dev/null || true)" ]; then
      docker compose images --format json > "${SNAP}/images.json" 2>/dev/null || true
      for img in $(docker compose images -q 2>/dev/null | sort -u); do docker tag "$img" "stoic-rollback:${TS}-${img:0:12}" 2>/dev/null || true; done
      [ -x deploy/backup.sh ] && BACKUP_DIR="${SNAP}/db" bash deploy/backup.sh backup </dev/null >/dev/null 2>&1 && echo "-- DB dumped to ${SNAP}/db" || echo "-- DB dump skipped (stack not running or backup.sh unavailable)"
    fi )
  echo "-- snapshot: ${SNAP}"
}

rollback() {
  local rc=$?
  trap - ERR
  echo
  echo "!! FAILED at step '${STEP}' (exit ${rc}) — log: ${LOG}"
  if [ -d "${TARGET}" ] && [ -x "${TARGET}/deploy/doctor.sh" ]; then
    bash "${TARGET}/deploy/doctor.sh" --bundle >/dev/null 2>&1 && echo "!! diagnostics bundle written (see deploy/doctor.sh --bundle output in ${TARGET}/diagnostics)" || true
  fi
  if [ "${NO_ROLLBACK}" = 1 ] || [ -z "${SNAP}" ] || [ ! -d "${SNAP}" ]; then
    echo "!! no rollback (fresh install or --no-rollback). Inspect: sudo bash ${TARGET}/deploy/doctor.sh"
    exit "${rc}"
  fi
  echo "!! rolling back to snapshot ${SNAP}"
  ( cd "${TARGET}"
    [ -f "${SNAP}/git_ref" ] && git checkout -q --detach "$(cat "${SNAP}/git_ref")" 2>/dev/null || true
    [ -f "${SNAP}/.env" ] && cp -a "${SNAP}/.env" .env
    [ -f "${SNAP}/backend_.env" ] && cp -a "${SNAP}/backend_.env" backend/.env
    [ -f "${SNAP}/secrets.tgz" ] && rm -rf secrets && tar -xzf "${SNAP}/secrets.tgz"
    if [ -f "${SNAP}/git_ref" ]; then
      docker compose build -q >/dev/null 2>&1 || true
      docker compose up -d >/dev/null 2>&1 || true
      for i in $(seq 1 30); do curl -fsS http://127.0.0.1:8001/api/health >/dev/null 2>&1 && { echo "!! previous release restored and healthy"; break; }; sleep 2; done
    fi )
  ls "${SNAP}"/db/stoic-mongo-*.archive.gz >/dev/null 2>&1 && echo "!! DB dump kept at ${SNAP}/db — restore only if data changed: deploy/backup.sh restore <file>"
  [ "${HAD_LOCK}" = 1 ] && lock_install "${TARGET}"
  echo "!! rollback finished. Fix the cause and re-run. Diagnostics: sudo bash ${TARGET}/deploy/doctor.sh"
  exit "${rc}"
}
trap rollback ERR

# ------------------------------------------------------------------ 0 · system check (read-only)
# Nothing is installed or modified until every check is green. FAIL stops here
# with the remediation; WARN continues (or stops with --strict).
STEP="system-check"
log "0/5 system check (read-only) — bootstrap.sh ${BOOTSTRAP_VERSION}"
# Read-only probes below pipe long outputs (ss, timedatectl, docker) into early-exiting
# readers; with pipefail a SIGPIPE on the writer (exit 141) would abort the whole run.
set +o pipefail
CK_FAIL=0; CK_WARN=0
pass() { printf '  \e[32mPASS\e[0m  %s\n' "$*"; }
warnc() { CK_WARN=$((CK_WARN+1)); printf '  \e[33mWARN\e[0m  %s\n' "$*"; }
failc() { CK_FAIL=$((CK_FAIL+1)); printf '  \e[31mFAIL\e[0m  %s\n' "$*"; }
reach() {  # any HTTP answer counts (registries reply 401 to anonymous probes); 000 = unreachable
  local code; code=$(curl -s -m 8 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || true); [ -n "${code}" ] && [ "${code}" != "000" ]; }
is_cloudflare_ip() { case "$1" in 172.6[4-9].*|172.7[01].*|104.1[6-9].*|104.2[0-9].*|104.3[01].*|188.114.*|141.101.*|108.162.*|162.15[89].*|198.41.*|190.93.*|197.234.*|131.0.7[2-5].*) return 0 ;; *) return 1 ;; esac; }

. /etc/os-release
case "${ID}:${VERSION_ID%%.*}" in
  almalinux:8|almalinux:9|rocky:8|rocky:9|rhel:8|rhel:9|centos:8|centos:9) pass "os: ${PRETTY_NAME} (RHEL family, supported)" ;;
  ubuntu:20|ubuntu:22|ubuntu:24|debian:11|debian:12|debian:13) pass "os: ${PRETTY_NAME} (Debian family, supported)" ;;
  *) if command -v dnf >/dev/null || command -v apt-get >/dev/null; then warnc "os: ${PRETTY_NAME} — untested release; continuing on a best-effort basis"
     else failc "os: ${PRETTY_NAME} — unsupported (need dnf or apt)"; fi ;;
esac
case "$(uname -m)" in x86_64|aarch64) pass "arch: $(uname -m)" ;; *) failc "arch: $(uname -m) — Docker images are built for x86_64/aarch64" ;; esac
command -v systemctl >/dev/null && pass "systemd present" || failc "systemd missing — Docker service and health timer need it"
[ "$(id -u)" = 0 ] && pass "running as root" || failc "not root"

CPU=$(nproc 2>/dev/null || echo 1); MEM_GB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
DISK_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9'); SWAP_GB=$(awk '/SwapTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
[ "${CPU}" -ge 2 ] && pass "cpu: ${CPU} cores" || warnc "cpu: ${CPU} core — 2+ recommended (API + 6 workers)"
if [ "${MEM_GB}" -ge 4 ]; then pass "memory: ${MEM_GB} GB"; elif [ "${MEM_GB}" -ge 3 ]; then warnc "memory: ${MEM_GB} GB — 4 GB recommended"; else failc "memory: ${MEM_GB} GB — below 3 GB the workers will be OOM-killed"; fi
if [ "${DISK_GB}" -ge 20 ]; then pass "disk: ${DISK_GB} GB free on /"; elif [ "${DISK_GB}" -ge 12 ]; then warnc "disk: ${DISK_GB} GB free — 20 GB recommended (images + Mongo + backups)"; else failc "disk: ${DISK_GB} GB free — need at least 12 GB"; fi
[ "${SWAP_GB}" -ge 1 ] || [ "${MEM_GB}" -ge 8 ] && pass "swap/memory headroom ok" || warnc "no swap and < 8 GB RAM — consider a 2 GB swapfile"

if timedatectl show 2>/dev/null | grep -q "NTPSynchronized=yes"; then pass "clock synchronised (NTP)"; else warnc "clock not NTP-synchronised — TLS/JWT/TOTP need a correct clock (enable chronyd)"; fi
if command -v getenforce >/dev/null; then pass "selinux: $(getenforce) (secrets/ will be labelled container_file_t)"; fi
if [ -f /proc/sys/fs/may_detach_mounts ]; then
  if [ "$(cat /proc/sys/fs/may_detach_mounts)" = 1 ]; then pass "kernel: fs.may_detach_mounts=1 (containers can be removed even when cPanel/CageFS or httpd hold their overlay mounts)"
  else warnc "kernel: fs.may_detach_mounts=0 — Docker cannot remove containers whose overlay mount leaked into another mount namespace (cPanel CageFS/LVE, httpd PrivateTmp): 'device or resource busy', dead containers keep their names; will be set to 1 persistently (/etc/sysctl.d/99-stoic-docker.conf)"; fi
fi
ZOMBIES=$(docker ps -a --format '{{.ID}} {{.Names}} {{.Status}}' 2>/dev/null | grep -Ec " [0-9a-f]{12}_stoic-| (Dead|Removal)" || true)
[ "${ZOMBIES}" -gt 0 ] && warnc "docker: ${ZOMBIES} dead/renamed stoic container(s) from a previous run — the installer will reap them before starting the stack" || true

reach https://download.docker.com && pass "network: download.docker.com reachable" || failc "network: cannot reach download.docker.com (Docker packages)"
reach https://registry-1.docker.io/v2/ && pass "network: Docker Hub reachable" || failc "network: cannot reach registry-1.docker.io (base images)"
reach https://pypi.org/simple/ && pass "network: PyPI reachable" || failc "network: cannot reach pypi.org (backend build)"
reach https://registry.yarnpkg.com && pass "network: yarn registry reachable" || failc "network: cannot reach registry.yarnpkg.com (frontend build)"
if [ -n "${REPO}" ]; then
  git ls-remote -q "${REPO}" HEAD >/dev/null 2>&1 && pass "network: repository reachable (${REPO})" || failc "network: cannot read ${REPO} (URL / credentials?)"
fi

if [ "${PUBLIC}" = 1 ]; then
  PUB_IP=$(curl -fs -m 5 https://api.ipify.org 2>/dev/null || curl -fs -m 5 https://ifconfig.me 2>/dev/null || echo "?")
  DNS_IP=$(getent ahostsv4 "${DOMAIN}" 2>/dev/null | awk 'NR==1{print $1}' || echo "")
  # cPanel hosts carry several public IPs (server main IP ≠ the account's dedicated IP):
  # an A record pointing at ANY address configured on this host is "this host"
  HOST_IPS=" $(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | tr '\n' ' ')${PUB_IP} "
  if [ -z "${DNS_IP}" ]; then failc "dns: ${DOMAIN} does not resolve — create an A record → ${PUB_IP} first (Caddy cannot issue the certificate otherwise)"
  elif case "${HOST_IPS}" in *" ${DNS_IP} "*) true ;; *) false ;; esac; then pass "dns: ${DOMAIN} → ${DNS_IP} (this host$([ "${DNS_IP}" != "${PUB_IP}" ] && echo ", dedicated IP — main IP ${PUB_IP}"))"
  elif [ "${CLOUDFLARE}" = 1 ] && is_cloudflare_ip "${DNS_IP}"; then pass "dns: ${DOMAIN} → ${DNS_IP} (Cloudflare proxy, orange cloud — expected in --cloudflare mode; origin record must point at ${PUB_IP})"
  elif [ "${CLOUDFLARE}" = 1 ]; then warnc "dns: ${DOMAIN} → ${DNS_IP} is not a Cloudflare edge — --cloudflare expects the record to be proxied (orange); the Origin CA certificate is NOT trusted by browsers when Cloudflare is bypassed"
  elif is_cloudflare_ip "${DNS_IP}"; then warnc "dns: ${DOMAIN} → ${DNS_IP} (Cloudflare proxy) — the origin record must point to ${PUB_IP}; set Cloudflare SSL to 'Full (strict)' and grey-cloud the record during the first certificate issue (Caddy HTTP-01), then re-enable the proxy — or use --cloudflare <domain> with an Origin CA certificate to stay orange"
  elif [ "${PUB_IP}" != "?" ] && [ "${DNS_IP}" != "${PUB_IP}" ] && [ "${MODE}" = "--behind-proxy" ]; then warnc "dns: ${DOMAIN} → ${DNS_IP} but this host is ${PUB_IP} — fine for now (install is verified on loopback via Apache); point the A record here at cut-over"
  elif [ "${PUB_IP}" != "?" ] && [ "${DNS_IP}" != "${PUB_IP}" ]; then failc "dns: ${DOMAIN} → ${DNS_IP} but this host is ${PUB_IP} — point the A record at this host"
  else pass "dns: ${DOMAIN} → ${DNS_IP} (this host)"; fi
  for p in 80 443; do
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}$"; then
      OWNER=$(ss -ltnp 2>/dev/null | awk -v P="[:.]${p}$" '$4 ~ P && !seen {print $6; seen=1}' | sed -E 's/.*\("([^"]+)".*/\1/')
      if [ "${MODE}" = "--behind-proxy" ]; then pass "port ${p}: ${OWNER:-web server} owns it and will reverse-proxy to STOIC (behind-proxy mode)"
      elif docker compose ps 2>/dev/null | grep -q caddy; then pass "port ${p}: held by the existing STOIC stack (upgrade)"
      elif [ "${OWNER}" = httpd ] || [ "${OWNER}" = nginx ] || [ "${OWNER}" = apache2 ]; then failc "port ${p}: in use by ${OWNER} — this host already runs a web server. Either stop it, or install STOIC behind it: re-run with --behind-proxy ${DOMAIN}"
      else failc "port ${p}: in use by another service ($(ss -ltnp 2>/dev/null | awk -v P=":${p}" '$4 ~ P"$" && !seen {print $6; seen=1}' | sed 's/users:((\"\([^\"]*\)\".*/\1/')) — stop it (httpd/nginx) or move it"; fi
    else
      [ "${MODE}" = "--behind-proxy" ] && warnc "port ${p}: nothing listening — --behind-proxy expects your web server on 80/443 (or use --production for built-in TLS)" || pass "port ${p}: free"
    fi
  done
fi
for p in 8001 3000 27017; do
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}$" && ! docker ps --format '{{.Ports}}' 2>/dev/null | grep -q ":${p}->"; then
    warnc "port ${p}: in use by a non-Docker service — the stack binds it on 127.0.0.1"; fi
done
# MongoDB is a container (mongo:7 in docker-compose.yml) — a host mongod is neither needed nor wanted
if command -v mongod >/dev/null || systemctl is-active -q mongod 2>/dev/null; then
  warnc "mongodb: host mongod found ($(mongod --version 2>/dev/null | sed -n 1p | awk '{print $3}')) — STOIC runs its own mongo:7 container; stop/disable the host service so it does not take 127.0.0.1:27017"
else pass "mongodb: runs in Docker (mongo:7 container, 127.0.0.1 only) — no host install needed"; fi
if ls /etc/yum.repos.d/mongodb-org-*.repo >/dev/null 2>&1; then
  for rf in /etc/yum.repos.d/mongodb-org-*.repo; do
    grep -q 'baseurl=https://repo.mongodb.org/' "${rf}" || warnc "mongodb: ${rf} has a broken baseurl — dnf will error on every run; remove it (rm -f ${rf} && dnf clean all)"; done
fi
if [ "${CLOUDFLARE}" = 1 ]; then
  if openssl x509 -in "${ORIGIN_CERT}" -noout >/dev/null 2>&1 \
     && [ "$(openssl x509 -in "${ORIGIN_CERT}" -noout -pubkey 2>/dev/null | openssl sha256)" = "$(openssl pkey -in "${ORIGIN_KEY}" -pubout 2>/dev/null | openssl sha256)" ]; then
    CF_SANS=$(openssl x509 -in "${ORIGIN_CERT}" -noout -ext subjectAltName 2>/dev/null | tr ',' '\n' | sed -n 's/.*DNS:\([^ ]*\).*/\1/p' | tr '\n' ' ')
    if echo " ${CF_SANS}" | grep -qE " (${DOMAIN}|\*\.${DOMAIN#*.}) "; then pass "cloudflare: origin certificate + key match, covers ${DOMAIN} (expires $(openssl x509 -in "${ORIGIN_CERT}" -noout -enddate | cut -d= -f2))"
    else failc "cloudflare: origin certificate does not cover ${DOMAIN} (SANs: ${CF_SANS:-none}) — create it for ${DOMAIN} and *.${DOMAIN}"; fi
    echo " ${CF_SANS}" | grep -qE " (www\.${DOMAIN}|\*\.${DOMAIN}) " || warnc "cloudflare: certificate does not cover www.${DOMAIN} — 'Full (strict)' will fail for the www host"
    case "$(openssl x509 -in "${ORIGIN_CERT}" -noout -issuer 2>/dev/null)" in *loud[fF]lare*) ;; *) warnc "cloudflare: certificate issuer is not Cloudflare Origin CA — Cloudflare 'Full (strict)' only trusts Origin CA or publicly trusted certificates" ;; esac
  else failc "cloudflare: ${ORIGIN_CERT} / ${ORIGIN_KEY} — not a PEM certificate, or the key does not match the certificate"; fi
  reach https://www.cloudflare.com/ips-v4 && pass "network: cloudflare.com edge IP list reachable" || warnc "network: cannot fetch Cloudflare edge IPs — bundled deploy/cloudflare/ips.txt will be used"
fi
is_podman_shim() {   # AlmaLinux's podman-docker ships /usr/bin/docker as a podman wrapper
  command -v docker >/dev/null || return 1
  local v; v=$(docker --version 2>&1 || true)
  case "${v}" in *[Pp]odman*) return 0 ;; esac
  case "$(rpm -qf "$(command -v docker)" 2>/dev/null || true)" in podman-docker*) return 0 ;; esac
  return 1
}
if command -v podman >/dev/null && ! command -v docker >/dev/null; then warnc "podman installed — it will be removed (conflicts with docker-ce on RHEL 8)"; fi
if is_podman_shim; then warnc "docker: /usr/bin/docker is the podman-docker shim ($(docker --version 2>&1 | sed -n 1p)) — not Docker Engine; podman/podman-docker/buildah/runc will be removed and Docker Engine + Compose v2 installed (the stack needs the real engine: compose v2, file secrets, health-gated depends_on)"
elif command -v docker >/dev/null; then docker info >/dev/null 2>&1 && pass "docker: present and running ($(docker --version | awk '{print $3}' | tr -d ,))" || warnc "docker: installed but daemon not running — will be started"; else pass "docker: not installed — will be installed"; fi

echo
echo "system check: ${CK_FAIL} FAIL · ${CK_WARN} WARN"
if [ "${CK_FAIL}" -gt 0 ] || { [ "${STRICT}" = 1 ] && [ "${CK_WARN}" -gt 0 ]; }; then
  trap - ERR
  echo "!! system check not green — nothing was changed on this host. Fix the items above and re-run."
  echo "   (re-check only: sudo bash deploy/bootstrap.sh ${MODE} ${DOMAIN} --check-only)"
  exit 2
fi
echo "-- all green — proceeding"
set -o pipefail
if [ "${CHECK_ONLY}" = 1 ]; then trap - ERR; echo "-- --check-only: stopping here, nothing changed"; exit 0; fi
if [ "${HAD_LOCK}" = 1 ]; then
  echo "-- --unlock: removing install lock ${LOCK_FILE} (re-created on success, restored on rollback)"
  chattr -i "${LOCK_FILE}" 2>/dev/null || true; rm -f "${LOCK_FILE}"
fi

# ------------------------------------------------------------------ 1 · prerequisites
STEP="prerequisites"
log "1/5 prerequisites"
. /etc/os-release
echo "-- host: ${PRETTY_NAME:-unknown} · kernel $(uname -r) · arch $(uname -m)"
FAMILY=""
if command -v dnf >/dev/null; then FAMILY=rhel
elif command -v apt-get >/dev/null; then FAMILY=debian
else echo "ERROR: unsupported package manager (need dnf or apt)"; exit 1; fi

if [ "${FAMILY}" = rhel ]; then
  dnf -y -q install ca-certificates curl git openssl tar gzip policycoreutils >/dev/null
  # RHEL/Alma 8 ships python3 = 3.6; the deploy scripts need 3.9+. Install a
  # modern interpreter and make it the `python3` used by the deploy scripts
  # (dnf itself uses /usr/libexec/platform-python and is unaffected).
  PYMAJ=$(python3 -c 'import sys;print(sys.version_info[1])' 2>/dev/null || echo 0)
  if [ "${PYMAJ}" -lt 9 ]; then
    dnf -y -q install python3.11 >/dev/null 2>&1 || dnf -y -q install python3.9 >/dev/null
    NEWPY=$(command -v python3.11 || command -v python3.9)
    alternatives --install /usr/bin/python3 python3 "${NEWPY}" 20 >/dev/null 2>&1 || true
    alternatives --set python3 "${NEWPY}" >/dev/null 2>&1 || ln -sf "${NEWPY}" /usr/local/bin/python3
    hash -r
  fi
  echo "-- python3: $(python3 --version 2>&1)"
  if is_podman_shim || ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    echo "-- installing Docker Engine + Compose v2 (docker-ce repo)"
    if is_podman_shim || command -v podman >/dev/null; then
      echo "-- removing podman / podman-docker shim (conflicts with docker-ce on RHEL 8)"
      dnf -y -q remove podman-docker podman buildah runc >/dev/null 2>&1 || true
      rm -f /usr/bin/docker /etc/containers/nodocker 2>/dev/null; hash -r
    fi
    dnf -y -q install dnf-plugins-core >/dev/null
    dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo >/dev/null
    dnf -y -q install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
    hash -r
  fi
  systemctl enable --now docker >/dev/null
  # RHEL 8 default fs.may_detach_mounts=0 makes `docker rm` fail with EBUSY when
  # a container's overlay mount leaked into another mount namespace (cPanel
  # CageFS/LVE, httpd PrivateTmp) — the container stays "dead", keeps its name
  # and every later `compose up` breaks. Docker's own 99-docker.conf is not
  # always applied on hosts where dockerd was installed after boot.
  if [ -f /proc/sys/fs/may_detach_mounts ] && [ "$(cat /proc/sys/fs/may_detach_mounts)" != 1 ]; then
    echo "-- enabling fs.may_detach_mounts=1 (persistent: /etc/sysctl.d/99-stoic-docker.conf)"
    printf 'fs.may_detach_mounts = 1\n' > /etc/sysctl.d/99-stoic-docker.conf
    sysctl -q -p /etc/sysctl.d/99-stoic-docker.conf
  fi
  if command -v getenforce >/dev/null; then echo "-- SELinux: $(getenforce)"; fi
else
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl git python3 openssl gnupg lsb-release >/dev/null
  if is_podman_shim || ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    echo "-- installing Docker Engine + Compose v2"
    if is_podman_shim; then echo "-- removing podman-docker shim"; apt-get remove -y -qq podman-docker >/dev/null 2>&1 || true; hash -r; fi
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true
fi
docker --version; docker compose version
docker info >/dev/null 2>&1 || { echo "ERROR: docker daemon not running"; exit 1; }
openssl genpkey -algorithm ed25519 -out /dev/null 2>/dev/null || { echo "ERROR: OpenSSL >= 1.1.1 with Ed25519 required"; exit 1; }


# ------------------------------------------------------------------ 2 · source
STEP="source"
log "2/5 source"
if [ -f "${HERE}/deploy/install.sh" ] && [ -f "${HERE}/docker-compose.yml" ] && [ -z "${REPO}" ]; then
  TARGET="${HERE}"; echo "-- using this checkout: ${TARGET}"
  snapshot
else
  [ -n "${REPO}" ] || { echo "ERROR: --repo <git url> is required when not run from a checkout"; exit 1; }
  if [ -d "${TARGET}/.git" ]; then
    snapshot
    echo "-- updating ${TARGET}"
    git -C "${TARGET}" fetch -q --tags --prune origin
    if [ -n "${REF}" ]; then git -C "${TARGET}" checkout -q -f --detach "${REF}"
    else
      # a previous rollback leaves HEAD detached — always realign to the remote default branch
      BR=$(git -C "${TARGET}" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##' || true); BR="${BR:-main}"
      git -C "${TARGET}" rev-parse -q --verify "origin/${BR}" >/dev/null 2>&1 || BR=master
      git -C "${TARGET}" checkout -q -f -B "${BR}" "origin/${BR}"
      echo "-- ${TARGET} at origin/${BR} $(git -C "${TARGET}" rev-parse --short HEAD)"
    fi
  else
    echo "-- cloning ${REPO} → ${TARGET}"
    git clone -q "${REPO}" "${TARGET}"
    [ -z "${REF}" ] || git -C "${TARGET}" checkout -q --detach "${REF}"
  fi
fi
cd "${TARGET}"
echo "   commit: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
chmod +x deploy/*.sh deploy/*/*.sh 2>/dev/null || true
if [ "${CLOUDFLARE}" = 1 ]; then
  # Origin CA certificate + key become Docker secrets (mode 600, gitignored) for Caddy
  mkdir -p secrets && chmod 700 secrets
  install -m 600 "${ORIGIN_CERT}" secrets/origin_cert.pem
  install -m 600 "${ORIGIN_KEY}" secrets/origin_key.pem
  command -v getenforce >/dev/null && [ "$(getenforce 2>/dev/null)" = "Enforcing" ] && chcon -Rt container_file_t secrets 2>/dev/null || true
  echo "-- cloudflare: origin certificate staged in secrets/origin_cert.pem + origin_key.pem"
fi

# ------------------------------------------------------------------ 3 · firewall / DNS
STEP="network"
log "3/5 network"
if [ "${PUBLIC}" = 1 ]; then
  if systemctl is-active --quiet firewalld 2>/dev/null; then
    firewall-cmd -q --permanent --add-service=http; firewall-cmd -q --permanent --add-service=https; firewall-cmd -q --reload
    echo "-- firewalld: http/https allowed"
  elif command -v ufw >/dev/null && ufw status 2>/dev/null | grep "Status: active" >/dev/null; then
    ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; echo "-- ufw: opened 80/443"
  else echo "-- no host firewall active (cloud security group must allow 80/443)"; fi
  echo "-- DNS / ports were verified in the system check (step 0)"
fi
if [ "${SKIP_ATTEST}" = 1 ]; then
  touch .env; grep -q '^ATTESTATION_REQUIRED=' .env && sed -i 's/^ATTESTATION_REQUIRED=.*/ATTESTATION_REQUIRED=false/' .env || echo 'ATTESTATION_REQUIRED=false' >> .env
  echo "!! attestation gate DISABLED (--skip-attestation): images are built from this checkout without a CI attestation record"
fi

# ------------------------------------------------------------------ 4 · install
STEP="install"
log "4/5 install (deploy/install.sh ${MODE} ${DOMAIN} ${EXTRA[*]:-})"
bash deploy/install.sh "${MODE}" ${DOMAIN:+"${DOMAIN}"} ${EXTRA[@]+"${EXTRA[@]}"}

# ------------------------------------------------------------------ 5 · verify + operations
STEP="verify"
log "5/5 verify + operations"
bash deploy/doctor.sh --quiet || { echo "ERROR: post-install diagnostics reported failures"; exit 1; }
CPANEL_WIRED=0
if [ "${MODE}" = "--behind-proxy" ] && [ -x /scripts/rebuildhttpdconf ]; then
  echo "-- cPanel/WHM detected: wiring the ${DOMAIN} vhost as a reverse proxy (deploy/proxy/cpanel.sh)"
  if bash deploy/proxy/cpanel.sh "${DOMAIN}"; then CPANEL_WIRED=1
  else echo "!! cPanel wiring failed (non-fatal — the stack is up). Re-run by hand: sudo bash ${TARGET}/deploy/proxy/cpanel.sh ${DOMAIN}"; fi
fi
if [ "${PUBLIC}" = 1 ] && [ -x deploy/backup.sh ]; then
  bash deploy/backup.sh schedule >/dev/null 2>&1 && echo "-- nightly Mongo backup scheduled" || echo "-- backup schedule skipped (run: deploy/backup.sh schedule)"
fi
# alert channels (values live in ./.env, mode 600 — never in the repo)
set_env() { touch .env; chmod 600 .env; grep -q "^$1=" .env && sed -i "s|^$1=.*|$1=$2|" .env || echo "$1=$2" >> .env; }
if [ -n "${TELEGRAM}" ]; then
  set_env HEALTHWATCH_TELEGRAM_BOT_TOKEN "${TELEGRAM%:*}"; set_env HEALTHWATCH_TELEGRAM_CHAT_ID "${TELEGRAM##*:}"
fi
[ -z "${REPORT_EMAIL}" ] || { set_env INSTALL_REPORT_EMAIL "${REPORT_EMAIL}"; set_env HEALTHWATCH_EMAIL "${REPORT_EMAIL}"; }
if [ "${PUBLIC}" = 1 ]; then
  bash deploy/healthwatch.sh install || echo "!! health watcher timer not installed (systemd missing?) — run: deploy/healthwatch.sh install"
fi
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD) bootstrap ${MODE#--} ${DOMAIN}" >> deploy/releases.log
lock_install "${TARGET}"
bash deploy/install_report.sh ${REPORT_EMAIL:+--email "${REPORT_EMAIL}"} || echo "!! install report step failed (non-fatal)"
trap - ERR
cat <<EOF

== STOIC is installed — installer LOCKED ==
   project:     ${TARGET}
   app:         $([ "${PUBLIC}" = 1 ] && echo "https://${DOMAIN}" || echo "http://127.0.0.1:3000 (loopback)")$([ "${MODE}" = "--behind-proxy" ] && { [ "${CPANEL_WIRED}" = 1 ] && echo " — Apache (cPanel) already proxies to STOIC on this host; cut over DNS when ready" || echo " — after you add deploy/proxy/apache-${DOMAIN}.conf (or nginx-…) to your web server; cPanel: sudo bash ${TARGET}/deploy/proxy/cpanel.sh ${DOMAIN}"; })
   admin login: ADMIN_EMAIL / ADMIN_PASSWORD in ${TARGET}/backend/.env — change it and enroll 2FA at first login
   diagnostics: sudo bash ${TARGET}/deploy/doctor.sh           (add --bundle to export a redacted support archive)
   database:    sudo bash ${TARGET}/deploy/doctor.sh --db      (MongoDB runs in Docker — no host mongod; this prints status/version/ping)
   backup now:  sudo bash ${TARGET}/deploy/doctor.sh --db --backup-now   (dump → encrypt → restore-verify in one go)
   upgrade:     sudo bash ${TARGET}/deploy/update.sh [tag|sha]  (backup → rebuild → verify → auto-rollback)
   rollback:    sudo bash ${TARGET}/deploy/rollback.sh          (previous release from deploy/releases.log)
   lock:        ${TARGET}/.stoic-installed — bootstrap.sh and install.sh refuse to run again; reinstall only with --unlock
   health:      hourly systemd timer → deploy/healthwatch.sh (status: deploy/healthwatch.sh status · test alert: deploy/healthwatch.sh test)
   report:      signed install report in ${TARGET}/deploy/releases/ (e-mailed when RESEND_API_KEY is set)
   logs:        cd ${TARGET} && docker compose logs -f backend
   this log:    ${LOG}
EOF
exit 0
}
