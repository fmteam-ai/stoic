#!/usr/bin/env bash
# STOIC installation diagnostics — answers "what is wrong with this host?"
#   deploy/doctor.sh              full report (PASS/WARN/FAIL per check, exit 1 on FAIL)
#   deploy/doctor.sh --quiet      only WARN/FAIL lines (used by bootstrap post-install)
#   deploy/doctor.sh --bundle     also write diagnostics/stoic-diag-<ts>.tar.gz
#                                 (redacted: secrets/ and env VALUES are never included)
set -uo pipefail
cd "$(dirname "$0")/.."
QUIET=0; BUNDLE=0
for a in "$@"; do case "$a" in --quiet) QUIET=1 ;; --bundle) BUNDLE=1 ;; esac; done
FAILS=0; WARNS=0
ok()   { [ "${QUIET}" = 1 ] || printf '  PASS  %s\n' "$*"; }
warn() { WARNS=$((WARNS+1)); printf '  WARN  %s\n' "$*"; }
fail() { FAILS=$((FAILS+1)); printf '  FAIL  %s\n' "$*"; }
hdr()  { [ "${QUIET}" = 1 ] || printf '\n[%s]\n' "$*"; }
envval() { grep -E "^$2=" "$1" 2>/dev/null | head -1 | cut -d= -f2-; }

hdr "host"
. /etc/os-release 2>/dev/null || true
ok "os: ${PRETTY_NAME:-unknown} · kernel $(uname -r) · $(uname -m)"
MEM_GB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo); DISK_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
[ "${MEM_GB}" -ge 3 ] && ok "memory ${MEM_GB} GB" || warn "memory ${MEM_GB} GB (< 4 GB — workers may be OOM-killed)"
[ "${DISK_GB}" -ge 10 ] && ok "disk ${DISK_GB} GB free" || fail "disk ${DISK_GB} GB free (< 10 GB)"
if command -v getenforce >/dev/null; then
  SE=$(getenforce); ok "selinux ${SE}"
  if [ "${SE}" = "Enforcing" ] && [ -d secrets ] && ! ls -Zd secrets 2>/dev/null | grep -q container_file_t; then
    warn "secrets/ not labelled container_file_t — containers may be denied: chcon -Rt container_file_t secrets"; fi
fi
command -v docker >/dev/null && ok "docker $(docker --version | awk '{print $3}' | tr -d ,)" || fail "docker not installed"
docker compose version >/dev/null 2>&1 && ok "compose $(docker compose version --short)" || fail "docker compose v2 missing"
docker info >/dev/null 2>&1 && ok "docker daemon running" || fail "docker daemon not running (systemctl start docker)"
openssl genpkey -algorithm ed25519 -out /dev/null 2>/dev/null && ok "openssl ed25519 capable" || fail "openssl lacks Ed25519 (need >= 1.1.1)"
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo none)
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null && ok "python3 ${PYV}" || fail "python3 ${PYV} — deploy scripts need >= 3.9 (RHEL 8: dnf install python3.11; alternatives --set python3 /usr/bin/python3.11)"

hdr "configuration"
for f in .env backend/.env; do [ -f "$f" ] && ok "$f present" || fail "$f missing (run deploy/install.sh)"; done
if [ -d secrets ]; then
  for s in mongo_url jwt_secret key_vault_master metrics_token order_auth_secret ledger_anchor_key signer_token signer_ed25519_key signer_cert.pem signer_cert_key.pem; do
    [ -s "secrets/$s" ] || fail "secrets/$s missing or empty"; done
  ok "secrets/ ($(ls secrets | wc -l) files, mode $(stat -c %a secrets))"
  [ "$(stat -c %a secrets)" = 700 ] || warn "secrets/ should be mode 700"
else fail "secrets/ missing"; fi
APP_ENV=$(envval backend/.env APP_ENV); [ -n "${APP_ENV}" ] && ok "APP_ENV=${APP_ENV}" || warn "APP_ENV not set"
if [ "${APP_ENV}" = production ]; then
  [ "$(envval backend/.env RELEASE_SIGNER)" = external ] && ok "RELEASE_SIGNER=external" || fail "production requires RELEASE_SIGNER=external (boot refuses local)"
  grep -q '^ED25519_SIGNING_KEY_B64=' backend/.env && fail "ED25519_SIGNING_KEY_B64 present in backend/.env (private key must not live in the API)" || ok "no private signing key in API env"
  [ "$(envval backend/.env ADMIN_MFA_ENFORCED)" = true ] && ok "ADMIN_MFA_ENFORCED=true" || fail "production requires ADMIN_MFA_ENFORCED=true"
  grep -qE '^(STEP_UP|RATE_LIMIT)_BYPASS_TOKEN=.+' backend/.env && fail "test bypass tokens present in backend/.env" || ok "no test bypass tokens"
  [ -n "$(envval backend/.env CORS_ORIGINS)" ] && ok "CORS_ORIGINS=$(envval backend/.env CORS_ORIGINS)" || fail "CORS_ORIGINS empty"
fi
DOMAIN=$(envval .env DOMAIN)
if [ -n "${DOMAIN}" ]; then
  PUB_IP=$(curl -fsS -m 5 https://api.ipify.org 2>/dev/null || echo "?"); DNS_IP=$(getent ahostsv4 "${DOMAIN}" 2>/dev/null | awk '{print $1; exit}')
  if [ -z "${DNS_IP}" ]; then fail "DNS: ${DOMAIN} does not resolve"
  elif [ "${PUB_IP}" != "?" ] && [ "${DNS_IP}" != "${PUB_IP}" ]; then warn "DNS: ${DOMAIN} → ${DNS_IP} but this host is ${PUB_IP}"
  else ok "DNS: ${DOMAIN} → ${DNS_IP}"; fi
fi

hdr "containers"
if docker compose ps >/dev/null 2>&1; then
  while read -r name state health; do
    case "${state}${health}" in
      *running*healthy*|running) ok "${name}: ${state} ${health}" ;;
      *running*starting*) warn "${name}: ${state} ${health}" ;;
      *) fail "${name}: ${state} ${health:-}" ;;
    esac
  done < <(docker compose ps --format '{{.Name}} {{.State}} {{.Health}}' 2>/dev/null)
  RESTARTS=$(docker compose ps -q 2>/dev/null | xargs -r docker inspect --format '{{.Name}} {{.RestartCount}}' 2>/dev/null | awk '$2>3')
  [ -z "${RESTARTS}" ] && ok "no crash-looping containers" || fail "restart loops: ${RESTARTS}"
else fail "docker compose project not found in $(pwd)"; fi

hdr "endpoints"
probe() { # probe <label> <url> [expect]
  local code; code=$(curl -s -m 15 -o /tmp/doctor_body -w '%{http_code}' "$2" 2>/dev/null || echo 000)
  if [ "${code}" = "${3:-200}" ]; then ok "$1 → ${code}"; else fail "$1 → ${code} $(head -c 160 /tmp/doctor_body 2>/dev/null | tr '\n' ' ')"; fi
}
probe "API /health"            http://127.0.0.1:8001/health
probe "API /api/health"        http://127.0.0.1:8001/api/health
probe "API /api/health/ready"  http://127.0.0.1:8001/api/health/ready
probe "frontend"               http://127.0.0.1:3000/
if [ -f /tmp/doctor_body ] && curl -s -m 15 http://127.0.0.1:8001/api/release-key -o /tmp/doctor_body; then
  grep -q '"mode": *"external"' /tmp/doctor_body && ok "release signer: external (sidecar)" || warn "release signer: $(grep -o '"mode": *"[a-z]*"' /tmp/doctor_body | head -1) — live signing not active"
fi
if [ -n "${DOMAIN}" ]; then probe "https://${DOMAIN}/api/health" "https://${DOMAIN}/api/health"; fi
SIGNER_ID=$(docker compose ps -q signer 2>/dev/null)
if [ -n "${SIGNER_ID}" ]; then
  docker exec "${SIGNER_ID}" python -c "import urllib.request,ssl;ctx=ssl.create_default_context(cafile='/run/secrets/signer_cert');urllib.request.urlopen('https://localhost:9443/healthz',context=ctx,timeout=5)" >/dev/null 2>&1 \
    && ok "signer sidecar TLS healthz" || fail "signer sidecar not answering over TLS"
fi
TOKEN=$(cat secrets/metrics_token 2>/dev/null || true)
if [ -n "${TOKEN}" ]; then
  RR=$(curl -s -m 20 -H "Authorization: Bearer ${TOKEN}" http://127.0.0.1:8001/api/ops/release-readiness 2>/dev/null || true)
  echo "${RR}" | grep -q '"ready": *true' && ok "release-readiness: ready" || warn "release-readiness not ready: $(echo "${RR}" | head -c 200 | tr '\n' ' ')"
fi

hdr "recent backend errors"
ERRS=$(docker compose logs backend --tail 400 2>/dev/null | grep -E "ERROR|Traceback|RuntimeError" | tail -5)
[ -z "${ERRS}" ] && ok "no recent errors in backend log" || { warn "recent backend errors:"; echo "${ERRS}" | sed 's/^/        /'; }

if [ "${BUNDLE}" = 1 ]; then
  TS=$(date -u +%Y%m%dT%H%M%SZ); OUT="diagnostics/stoic-diag-${TS}"; mkdir -p "${OUT}"
  { . /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-}"; uname -a; free -h; df -h /; docker --version; docker compose version; } > "${OUT}/host.txt" 2>&1
  docker compose ps > "${OUT}/compose_ps.txt" 2>&1; docker compose config --no-interpolate > "${OUT}/compose_config.yml" 2>/dev/null
  for svc in backend signer mongo frontend worker-trading worker-protection worker-reconciliation worker-analytics worker-model worker-tuning caddy; do
    docker compose logs "${svc}" --tail 500 > "${OUT}/log_${svc}.txt" 2>/dev/null || true; done
  # env KEYS only — values are redacted
  for f in .env backend/.env; do [ -f "$f" ] && sed -E 's/^([A-Za-z_][A-Za-z0-9_]*)=.*/\1=<redacted>/' "$f" > "${OUT}/$(echo "$f" | tr / _).keys"; done
  ls -la secrets > "${OUT}/secrets_listing.txt" 2>/dev/null; command -v getenforce >/dev/null && getenforce > "${OUT}/selinux.txt"
  journalctl -u docker --no-pager -n 200 > "${OUT}/journal_docker.txt" 2>/dev/null || true
  ls /var/log/stoic-bootstrap-*.log >/dev/null 2>&1 && cp "$(ls -t /var/log/stoic-bootstrap-*.log | head -1)" "${OUT}/bootstrap.log"
  tar -czf "${OUT}.tar.gz" -C diagnostics "$(basename "${OUT}")" && rm -rf "${OUT}"
  echo; echo "diagnostics bundle: ${OUT}.tar.gz (no secret values inside — safe to share)"
fi

echo
echo "doctor: ${FAILS} FAIL · ${WARNS} WARN"
[ "${FAILS}" = 0 ]
