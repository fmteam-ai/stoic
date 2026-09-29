#!/usr/bin/env bash
# STOIC zero-touch server bootstrap — run ONE command on a fresh Ubuntu/Debian
# host and get the whole project installed, verified and serving over HTTPS:
#
#   curl -fsSL https://raw.githubusercontent.com/<you>/<repo>/main/deploy/bootstrap.sh \
#     | sudo bash -s -- --production trade.example.com --repo https://github.com/<you>/<repo>.git
#
#   sudo bash deploy/bootstrap.sh --dev                    # from a checkout, loopback only
#
# What it does (idempotent — safe to re-run for upgrades):
#   1. installs Docker Engine + Compose v2, git, curl, python3, openssl
#   2. clones the repository to /opt/stoic (or updates it; or uses the checkout
#      it is run from) and optionally checks out --ref <tag|sha>
#   3. runs deploy/install.sh which generates every secret (Mongo, JWT, key
#      vault, metrics, ORDER_AUTH, LEDGER_ANCHOR, release-signer key + token +
#      TLS cert), builds the images, starts API + 6 workers + signer + Mongo +
#      frontend (+ Caddy TLS in production) and refuses to finish until the
#      release-readiness probe is green
#   4. opens 80/443 in ufw when ufw is active (production), schedules backups
set -euo pipefail

MODE=""; DOMAIN=""; REPO="${STOIC_REPO_URL:-}"; REF="${STOIC_REF:-}"; TARGET="${STOIC_HOME:-/opt/stoic}"
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --production) MODE="--production"; DOMAIN="${2:-}"; shift 2 ;;
    --dev) MODE="--dev"; shift ;;
    --repo) REPO="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --target) TARGET="$2"; shift 2 ;;
    --with-forecast|--registry) EXTRA+=("$1"); shift ;;
    -h|--help) sed -n 2,20p "$0"; exit 0 ;;
    *) echo "unknown argument: $1"; exit 1 ;;
  esac
done
[ -n "${MODE}" ] || { echo "ERROR: choose --production <domain> or --dev"; exit 1; }
[ "${MODE}" = "--dev" ] || [ -n "${DOMAIN}" ] || { echo "ERROR: --production needs a domain"; exit 1; }
[ "$(id -u)" = 0 ] || { echo "ERROR: run as root (sudo)"; exit 1; }

log() { printf '\n== %s ==\n' "$*"; }

log "1/4 prerequisites"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl git python3 openssl gnupg lsb-release >/dev/null
  if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    echo "-- installing Docker Engine + Compose v2"
    install -m 0755 -d /etc/apt/keyrings
    . /etc/os-release
    curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true
else
  command -v docker >/dev/null && docker compose version >/dev/null 2>&1 \
    || { echo "ERROR: non-apt host — install Docker Engine + Compose v2, git, python3, openssl first"; exit 1; }
fi
docker --version; docker compose version

log "2/4 source"
HERE="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd || true)"
if [ -f "${HERE}/deploy/install.sh" ] && [ -f "${HERE}/docker-compose.yml" ] && [ -z "${REPO}" ]; then
  TARGET="${HERE}"
  echo "-- using this checkout: ${TARGET}"
else
  [ -n "${REPO}" ] || { echo "ERROR: --repo <git url> is required when not run from a checkout"; exit 1; }
  if [ -d "${TARGET}/.git" ]; then
    echo "-- updating ${TARGET}"
    git -C "${TARGET}" fetch --tags --prune origin
    git -C "${TARGET}" checkout -q "${REF:-$(git -C "${TARGET}" rev-parse --abbrev-ref HEAD)}"
    [ -n "${REF}" ] || git -C "${TARGET}" pull -q --ff-only
  else
    echo "-- cloning ${REPO} → ${TARGET}"
    git clone -q "${REPO}" "${TARGET}"
    [ -z "${REF}" ] || git -C "${TARGET}" checkout -q "${REF}"
  fi
fi
cd "${TARGET}"
echo "   commit: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

log "3/4 install (deploy/install.sh ${MODE} ${DOMAIN} ${EXTRA[*]:-})"
if [ "${MODE}" = "--production" ] && command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; echo "-- ufw: opened 80/443"
fi
bash deploy/install.sh "${MODE}" ${DOMAIN:+"${DOMAIN}"} "${EXTRA[@]:-}"

log "4/4 operations"
if [ -x deploy/backup.sh ] && [ "${MODE}" = "--production" ]; then
  bash deploy/backup.sh schedule >/dev/null 2>&1 && echo "-- nightly Mongo backup scheduled (deploy/backup.sh)" || echo "-- backup schedule skipped (run deploy/backup.sh schedule manually)"
fi
cat <<EOF

== STOIC is installed ==
   project:   ${TARGET}
   upgrade:   sudo bash ${TARGET}/deploy/bootstrap.sh ${MODE} ${DOMAIN}
   status:    cd ${TARGET} && docker compose ps
   logs:      cd ${TARGET} && docker compose logs -f backend
   admin:     see ADMIN_EMAIL / ADMIN_PASSWORD in ${TARGET}/backend/.env (change after first login, enroll 2FA)
   signer:    local sidecar (deploy/signer) — key in ./secrets/signer_ed25519_key, never in the API
   docs:      docs/DEPLOYMENT.md · docs/PRODUCTION_DEPLOY_CHECKLIST.md
EOF
