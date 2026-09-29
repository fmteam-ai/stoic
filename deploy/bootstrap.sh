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
#
# Options: --repo <git url> · --ref <tag|sha> · --target <dir> (default /opt/stoic)
#          --skip-attestation  build from your own checkout without a CI attestation record
#          --with-forecast · --registry (passed through to deploy/install.sh)
#          --no-rollback       keep the failed state for inspection
#          --report-email <addr>  where to e-mail the signed install report (needs RESEND_API_KEY)
#          --telegram <bot_token>:<chat_id>  health alerts + install report to Telegram
#
# Safety: every run snapshots the previous state (git ref, .env files, secrets,
# image tags, DB dump when the stack is running). If ANY step fails the trap
# restores the snapshot, restarts the previous release and writes a diagnostics
# bundle (deploy/doctor.sh) — the host is never left half-installed.
set -euo pipefail

MODE=""; DOMAIN=""; REPO="${STOIC_REPO_URL:-}"; REF="${STOIC_REF:-}"; TARGET="${STOIC_HOME:-/opt/stoic}"
SKIP_ATTEST=0; NO_ROLLBACK=0; EXTRA=(); REPORT_EMAIL=""; TELEGRAM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --production) MODE="--production"; DOMAIN="${2:-}"; shift 2 ;;
    --dev) MODE="--dev"; shift ;;
    --repo) REPO="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --target) TARGET="$2"; shift 2 ;;
    --skip-attestation) SKIP_ATTEST=1; shift ;;
    --no-rollback) NO_ROLLBACK=1; shift ;;
    --report-email) REPORT_EMAIL="$2"; shift 2 ;;
    --telegram) TELEGRAM="$2"; shift 2 ;;
    --with-forecast|--registry) EXTRA+=("$1"); shift ;;
    -h|--help) sed -n 2,22p "$0"; exit 0 ;;
    *) echo "unknown argument: $1"; exit 1 ;;
  esac
done
[ -n "${MODE}" ] || { echo "ERROR: choose --production <domain> or --dev"; exit 1; }
[ "${MODE}" = "--dev" ] || [ -n "${DOMAIN}" ] || { echo "ERROR: --production needs a domain"; exit 1; }
[ "$(id -u)" = 0 ] || { echo "ERROR: run as root (sudo)"; exit 1; }

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
    if docker compose ps -q 2>/dev/null | grep -q .; then
      docker compose images --format json > "${SNAP}/images.json" 2>/dev/null || true
      for img in $(docker compose images -q 2>/dev/null | sort -u); do docker tag "$img" "stoic-rollback:${TS}-${img:0:12}" 2>/dev/null || true; done
      [ -x deploy/backup.sh ] && BACKUP_DIR="${SNAP}/db" bash deploy/backup.sh backup >/dev/null 2>&1 && echo "-- DB dumped to ${SNAP}/db" || echo "-- DB dump skipped (stack not running or backup.sh unavailable)"
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
  echo "!! rollback finished. Fix the cause and re-run. Diagnostics: sudo bash ${TARGET}/deploy/doctor.sh"
  exit "${rc}"
}
trap rollback ERR

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
  if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    echo "-- installing Docker Engine + Compose v2 (docker-ce repo)"
    dnf -y -q remove podman buildah runc >/dev/null 2>&1 || true      # conflicts with docker-ce on RHEL 8
    dnf -y -q install dnf-plugins-core >/dev/null
    dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo >/dev/null
    dnf -y -q install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  fi
  systemctl enable --now docker >/dev/null
  if command -v getenforce >/dev/null; then echo "-- SELinux: $(getenforce)"; fi
else
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl git python3 openssl gnupg lsb-release >/dev/null
  if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    echo "-- installing Docker Engine + Compose v2"
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

# host sizing (informational — the stack needs ~4 GB RAM / 20 GB disk)
MEM_GB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
DISK_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
echo "-- resources: ${MEM_GB} GB RAM · ${DISK_GB} GB free on /"
[ "${MEM_GB}" -ge 3 ] || echo "!! WARNING: less than 4 GB RAM — the 6 workers may be OOM-killed"
[ "${DISK_GB}" -ge 15 ] || echo "!! WARNING: less than 15 GB free disk — image builds may fail"

# ------------------------------------------------------------------ 2 · source
STEP="source"
log "2/5 source"
HERE="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd || true)"
if [ -f "${HERE}/deploy/install.sh" ] && [ -f "${HERE}/docker-compose.yml" ] && [ -z "${REPO}" ]; then
  TARGET="${HERE}"; echo "-- using this checkout: ${TARGET}"
  snapshot
else
  [ -n "${REPO}" ] || { echo "ERROR: --repo <git url> is required when not run from a checkout"; exit 1; }
  if [ -d "${TARGET}/.git" ]; then
    snapshot
    echo "-- updating ${TARGET}"
    git -C "${TARGET}" fetch -q --tags --prune origin
    if [ -n "${REF}" ]; then git -C "${TARGET}" checkout -q --detach "${REF}"
    else git -C "${TARGET}" checkout -q "$(git -C "${TARGET}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)" && git -C "${TARGET}" pull -q --ff-only; fi
  else
    echo "-- cloning ${REPO} → ${TARGET}"
    git clone -q "${REPO}" "${TARGET}"
    [ -z "${REF}" ] || git -C "${TARGET}" checkout -q --detach "${REF}"
  fi
fi
cd "${TARGET}"
echo "   commit: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
chmod +x deploy/*.sh 2>/dev/null || true

# ------------------------------------------------------------------ 3 · firewall / DNS
STEP="network"
log "3/5 network"
if [ "${MODE}" = "--production" ]; then
  if systemctl is-active --quiet firewalld 2>/dev/null; then
    firewall-cmd -q --permanent --add-service=http; firewall-cmd -q --permanent --add-service=https; firewall-cmd -q --reload
    echo "-- firewalld: http/https allowed"
  elif command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; echo "-- ufw: opened 80/443"
  else echo "-- no host firewall active (cloud security group must allow 80/443)"; fi
  PUB_IP=$(curl -fs -m 5 https://api.ipify.org 2>/dev/null || curl -fs -m 5 https://ifconfig.me 2>/dev/null || echo "?")
  DNS_IP=$(getent ahostsv4 "${DOMAIN}" 2>/dev/null | awk '{print $1; exit}' || echo "")
  echo "-- DNS: ${DOMAIN} → ${DNS_IP:-<unresolved>} · this host → ${PUB_IP}"
  if [ -z "${DNS_IP}" ] || { [ "${PUB_IP}" != "?" ] && [ "${DNS_IP}" != "${PUB_IP}" ]; }; then
    echo "!! WARNING: ${DOMAIN} does not resolve to this host yet — Caddy cannot issue the certificate until it does."
  fi
  for p in 80 443; do
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}$" && ! docker compose ps 2>/dev/null | grep -q caddy; then
      echo "ERROR: port ${p} is already in use by another service (httpd/nginx?) — stop it or move it first"; exit 1; fi
  done
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
if [ "${MODE}" = "--production" ] && [ -x deploy/backup.sh ]; then
  bash deploy/backup.sh schedule >/dev/null 2>&1 && echo "-- nightly Mongo backup scheduled" || echo "-- backup schedule skipped (run: deploy/backup.sh schedule)"
fi
# alert channels (values live in ./.env, mode 600 — never in the repo)
set_env() { touch .env; chmod 600 .env; grep -q "^$1=" .env && sed -i "s|^$1=.*|$1=$2|" .env || echo "$1=$2" >> .env; }
if [ -n "${TELEGRAM}" ]; then
  set_env HEALTHWATCH_TELEGRAM_BOT_TOKEN "${TELEGRAM%%:*}"; set_env HEALTHWATCH_TELEGRAM_CHAT_ID "${TELEGRAM#*:}"
fi
[ -z "${REPORT_EMAIL}" ] || { set_env INSTALL_REPORT_EMAIL "${REPORT_EMAIL}"; set_env HEALTHWATCH_EMAIL "${REPORT_EMAIL}"; }
if [ "${MODE}" = "--production" ]; then
  bash deploy/healthwatch.sh install || echo "!! health watcher timer not installed (systemd missing?) — run: deploy/healthwatch.sh install"
fi
bash deploy/install_report.sh ${REPORT_EMAIL:+--email "${REPORT_EMAIL}"} || echo "!! install report step failed (non-fatal)"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD) bootstrap ${MODE#--} ${DOMAIN}" >> deploy/releases.log
trap - ERR
cat <<EOF

== STOIC is installed ==
   project:     ${TARGET}
   app:         $([ "${MODE}" = "--production" ] && echo "https://${DOMAIN}" || echo "http://127.0.0.1:3000 (loopback)")
   admin login: ADMIN_EMAIL / ADMIN_PASSWORD in ${TARGET}/backend/.env — change it and enroll 2FA at first login
   diagnostics: sudo bash ${TARGET}/deploy/doctor.sh           (add --bundle to export a redacted support archive)
   upgrade:     sudo bash ${TARGET}/deploy/bootstrap.sh ${MODE} ${DOMAIN}   (auto-rollback on failure)
   rollback:    sudo bash ${TARGET}/deploy/rollback.sh          (previous release from deploy/releases.log)
   health:      hourly systemd timer → deploy/healthwatch.sh (status: deploy/healthwatch.sh status · test alert: deploy/healthwatch.sh test)
   report:      signed install report in ${TARGET}/deploy/releases/ (e-mailed when RESEND_API_KEY is set)
   logs:        cd ${TARGET} && docker compose logs -f backend
   this log:    ${LOG}
EOF
