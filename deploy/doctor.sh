#!/usr/bin/env bash
# STOIC installation diagnostics — answers "what is wrong with this host?"
#   deploy/doctor.sh              full report (PASS/WARN/FAIL per check, exit 1 on FAIL)
#   deploy/doctor.sh --quiet      only WARN/FAIL lines (used by bootstrap post-install)
#   deploy/doctor.sh --bundle     also write diagnostics/stoic-diag-<ts>.tar.gz
#                                 (redacted: secrets/ and env VALUES are never included)
#   deploy/doctor.sh --db         MongoDB quick check only: container status, server
#                                 version, ping, app-user auth round trip, transaction
#                                 capability, data volume size (MongoDB runs in Docker —
#                                 `mongod` is NOT installed on the host, by design)
#   deploy/doctor.sh --db --backup-now
#                                 …then dump NOW (deploy/backup.sh: AES-256 when
#                                 BACKUP_PASSPHRASE_FILE is set), restore the archive into a
#                                 throw-away mongo:7 and compare every collection count —
#                                 prints the verified file, exit 1 on any mismatch
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/stoic-path.sh"   # private python >= 3.9 (bootstrap.sh) — audit H2 guarded PATH prepend
cd "$(dirname "$0")/.."
QUIET=0; BUNDLE=0; DBONLY=0; BACKUP_NOW=0
for a in "$@"; do case "$a" in --quiet) QUIET=1 ;; --bundle) BUNDLE=1 ;; --db) DBONLY=1 ;; --backup-now) DBONLY=1; BACKUP_NOW=1 ;; esac; done
FAILS=0; WARNS=0
ok()   { [ "${QUIET}" = 1 ] || printf '  PASS  %s\n' "$*"; }
warn() { WARNS=$((WARNS+1)); printf '  WARN  %s\n' "$*"; }
fail() { FAILS=$((FAILS+1)); printf '  FAIL  %s\n' "$*"; }
hdr()  { [ "${QUIET}" = 1 ] || printf '\n[%s]\n' "$*"; }
envval() { grep -E "^$2=" "$1" 2>/dev/null | head -1 | cut -d= -f2-; }

# mongosh inside the mongo container; the app password is read from the container's
# own /run/secrets mount so it never appears in a host process list or in this output
mongo_eval() { docker compose exec -T mongo sh -c 'mongosh --quiet -u "$MONGO_APP_USER" -p "$(cat /run/secrets/mongo_app_password)" --authenticationDatabase "$DB_NAME" "$DB_NAME" --eval "$0"' "$1" 2>/dev/null; }

db_check() {
  hdr "mongodb (Docker container — no host mongod by design)"
  command -v mongod >/dev/null && warn "host mongod present — unused by STOIC; make sure it is not bound to 127.0.0.1:27017" || ok "no host mongod (expected: MongoDB is the mongo:7 container)"
  local MID; MID=$(docker compose ps -q mongo 2>/dev/null)
  if [ -z "${MID}" ]; then fail "mongo container not found — stack not up? (docker compose up -d mongo)"; return; fi
  local STATE HEALTH IMAGE STARTED RESTARTS
  read -r STATE HEALTH IMAGE STARTED RESTARTS < <(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.Config.Image}} {{.State.StartedAt}} {{.RestartCount}}' "${MID}")
  case "${STATE}:${HEALTH}" in
    running:healthy|running:none) ok "container: ${STATE} (${HEALTH}) · image ${IMAGE} · up since ${STARTED%%.*} · restarts ${RESTARTS}" ;;
    running:*) warn "container: ${STATE} (${HEALTH}) · image ${IMAGE}" ;;
    *) fail "container: ${STATE} (${HEALTH}) · image ${IMAGE} — docker compose logs mongo --tail 50"; return ;;
  esac
  [ "${RESTARTS:-0}" -le 3 ] || fail "container restarted ${RESTARTS} times — check docker compose logs mongo"
  local VER; VER=$(docker compose exec -T mongo mongosh --quiet --eval 'db.version()' 2>/dev/null | tail -1)
  [ -n "${VER}" ] && ok "server version: ${VER}" || fail "cannot query server version (mongosh inside container failed)"
  local PING; PING=$(docker compose exec -T mongo mongosh --quiet --eval 'db.adminCommand({ping:1}).ok' 2>/dev/null | tail -1)
  [ "${PING}" = 1 ] && ok "ping: ok" || fail "ping failed (${PING:-no answer})"
  local RT; RT=$(mongo_eval 'const c=db.getCollection("_doctor_probe");c.insertOne({t:new Date()});const n=c.countDocuments({});c.drop();print("rt-ok "+n)' | tail -1)
  case "${RT}" in rt-ok*) ok "app user auth + write/read round trip on $(envval .env DB_NAME): ok" ;; *) fail "app user round trip failed: ${RT:-no answer} (secrets/mongo_app_password vs deploy/mongo-init.js?)" ;; esac
  # rs.status() needs clusterMonitor → authenticate as root (unauthenticated it throws → false "standalone")
  local RS; RS=$(docker compose exec -T mongo sh -c 'mongosh --quiet -u "$MONGO_INITDB_ROOT_USERNAME" -p "$(cat /run/secrets/mongo_root_password)" --eval "try{print(rs.status().set)}catch(e){print(\"standalone \"+e.codeName)}"' 2>/dev/null | tail -1)
  local APP_ENV; APP_ENV=$(envval backend/.env APP_ENV)
  if [ "${RS%% *}" = standalone ] || [ -z "${RS}" ]; then
    if [ "${APP_ENV}" = production ]; then fail "replica set: ${RS:-standalone} — production requires transactions (docs/PRODUCTION_DEPLOY_CHECKLIST.md → 'MongoDB transactions')"
    else warn "replica set: ${RS:-standalone} — transactions unavailable; required before any live-enabled account (see PRODUCTION_DEPLOY_CHECKLIST.md)"; fi
  else ok "replica set: ${RS} (transactions available)"; fi
  local COLLS; COLLS=$(mongo_eval 'print(db.getCollectionNames().length)' | tail -1)
  [ -n "${COLLS}" ] && ok "collections in $(envval .env DB_NAME): ${COLLS}"
  local VOL SIZE; VOL=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data/db"}}{{.Name}}{{end}}{{end}}' "${MID}")
  if [ -n "${VOL}" ]; then
    SIZE=$(docker system df -v 2>/dev/null | awk -v v="${VOL}" '$1==v {print $3; exit}')
    ok "data volume: ${VOL} (${SIZE:-size n/a}) — backups: deploy/backup.sh"
  fi
  local PORT; PORT=$(docker port "${MID}" 27017 2>/dev/null | head -1)
  if [ -z "${PORT}" ]; then ok "network: internal docker network only (not published)"
  elif [[ "${PORT}" == 127.0.0.1:* ]]; then ok "network: published on ${PORT} (loopback only)"
  else fail "network: published on ${PORT} — MongoDB must never be reachable from outside (bind 127.0.0.1)"; fi
}

backup_now() {
  hdr "backup now (dump → encrypt → restore into scratch mongo:7 → compare counts)"
  [ "${FAILS}" = 0 ] || { fail "skipping backup: the database check above reported ${FAILS} FAIL"; return; }
  # BACKUP_* settings live in ./.env (compose config); export them for backup.sh unless already set
  for k in BACKUP_DIR BACKUP_PASSPHRASE_FILE BACKUP_OFFSITE BACKUP_RCLONE_REMOTE BACKUP_S3_URI RETENTION_DAYS; do
    v=$(envval .env "$k"); [ -n "$v" ] && [ -z "${!k:-}" ] && export "$k=$v"; done
  local BDIR="${BACKUP_DIR:-./backups}" OUT
  if [ -n "${BACKUP_PASSPHRASE_FILE:-}" ]; then
    [ -s "${BACKUP_PASSPHRASE_FILE}" ] && ok "encryption: AES-256-CBC with passphrase file ${BACKUP_PASSPHRASE_FILE}" \
      || { fail "BACKUP_PASSPHRASE_FILE=${BACKUP_PASSPHRASE_FILE} missing or empty — refusing to write an unencrypted archive"; return; }
  else warn "encryption: OFF (set BACKUP_PASSPHRASE_FILE in ./.env to encrypt archives at rest)"; fi
  local LOG; LOG=$(mktemp /tmp/stoic-backup-now-XXXXXX)
  if ! bash deploy/backup.sh backup >"${LOG}" 2>&1; then fail "dump failed:"; sed 's/^/        /' "${LOG}" | tail -8; rm -f "${LOG}"; return; fi
  OUT=$(ls -t "${BDIR}"/stoic-mongo-*.archive.gz* 2>/dev/null | grep -v manifest | sed -n 1p)
  [ -n "${OUT}" ] && [ -s "${OUT}" ] || { fail "dump produced no archive in ${BDIR}"; rm -f "${LOG}"; return; }
  ok "dump: ${OUT} ($(du -h "${OUT}" | cut -f1)) · manifest $(python3 -c "import json;d=json.load(open('${OUT%.enc}.manifest.json'));print(sum(len(v) for v in d['databases'].values()),'collections /',sum(sum(v.values()) for v in d['databases'].values()),'documents')" 2>/dev/null || echo written)"
  if bash deploy/backup.sh verify "${OUT}" >"${LOG}" 2>&1; then ok "restore verification: $(grep -o 'RESTORE VERIFICATION PASSED.*' "${LOG}" | head -1)"
  else fail "restore verification FAILED for ${OUT}:"; grep -E "^  -|FAILED|ERROR|Error" "${LOG}" | head -10 | sed 's/^/        /'; fi
  rm -f "${LOG}"
  [ "${FAILS}" = 0 ] && echo "  verified backup: $(readlink -f "${OUT}")"
}

if [ "${DBONLY}" = 1 ]; then
  db_check
  [ "${BACKUP_NOW}" = 1 ] && backup_now
  LABEL="--db"; [ "${BACKUP_NOW}" = 1 ] && LABEL="--db --backup-now"
  echo; echo "doctor ${LABEL}: ${FAILS} FAIL · ${WARNS} WARN"
  exit $(( FAILS > 0 ))
fi

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
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null && ok "python3 ${PYV} ($(command -v python3))" || fail "python3 ${PYV} — deploy scripts need >= 3.9 (RHEL 8: dnf install python3.11 && deploy/host-python-fix.sh --apply — never repoint /usr/bin/python3 via alternatives, it breaks cPanel)"
if command -v dnf >/dev/null && [ -x /usr/bin/python3 ]; then
  /usr/bin/python3 -c 'import dnf' >/dev/null 2>&1 && ok "system /usr/bin/python3 $(/usr/bin/python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])') has the dnf module (cPanel packman OK)" \
    || fail "system /usr/bin/python3 lacks the dnf module — cPanel/WHM packman breaks (ModuleNotFoundError: dnf); fix: deploy/host-python-fix.sh --apply"
fi

# shared web host (cPanel/WHM, Plesk, DirectAdmin, httpd/exim/dovecot): fine for demo-only, blocks live trading
MARKERS=$(. deploy/lib.sh; . deploy/preflight.sh; shared_web_host_markers)
if [ -n "${MARKERS}" ]; then
  if [ "$(envval backend/.env APP_ENV)" = production ]; then warn "host suitability: STOIC shares this host with a public web/mail stack (${MARKERS}) — readiness BLOCKS live trading (demo-only: warning); migrate to a dedicated host: docs/HOST_MIGRATION.md"
  else warn "host suitability: shared web host detected (${MARKERS}) — acceptable for demo-only; plan a dedicated host before live trading (docs/HOST_MIGRATION.md)"; fi
else ok "host suitability: dedicated host (no cPanel/Plesk/DirectAdmin/httpd/exim/dovecot)"; fi
# host prerequisites that EXISTING installs may lack (applied by update.sh/restart.sh preflight or `make host-prereqs`)
if [ "$(id -u)" = 0 ]; then
  if bash deploy/host-prereqs.sh --check >/dev/null 2>&1; then ok "host prerequisites: fs.may_detach_mounts=1 · docker root slave · drop-ins present · host-profile timer active"
  else fail "host prerequisites missing (fs.may_detach_mounts / docker root propagation / stoic-host-profile.timer) — fix: sudo bash deploy/host-prereqs.sh --yes (update.sh applies it automatically)"; fi
  # M117-1 — the signed host profile expires 24 h after the last refresh; a missing/failed timer blocks live trading daily
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet stoic-host-profile.timer 2>/dev/null; then
      if systemctl is-failed --quiet stoic-host-profile.service 2>/dev/null; then fail "host-profile refresh: last run FAILED (journalctl -u stoic-host-profile.service) — profile expires 24 h after the last good write"
      else ok "host-profile timer: active (next: $(systemctl list-timers stoic-host-profile.timer --no-legend 2>/dev/null | awk '{print $1" "$2" "$3}' | head -1))"; fi
    else fail "host-profile timer: stoic-host-profile.timer not active — sudo bash deploy/host-prereqs.sh --yes (live trading blocks when the profile is >24 h old)"; fi
  fi
  if [ -f deploy/state/host_profile.json ]; then
    AGE_H=$(python3 -c 'import json, sys, time, calendar; d = json.load(open(sys.argv[1])); t = calendar.timegm(time.strptime(d["detected_at"], "%Y-%m-%dT%H:%M:%SZ")); print(round((time.time() - t) / 3600, 1))' deploy/state/host_profile.json 2>/dev/null || echo "?")
    case "${AGE_H}" in
      "?") warn "host profile file: deploy/state/host_profile.json unparseable — readiness reports it unverified" ;;
      *) if python3 -c 'import sys; sys.exit(0 if float(sys.argv[1]) > 24 else 1)' "${AGE_H}" 2>/dev/null; then fail "host profile file: ${AGE_H} h old (max 24 h) — UNVERIFIED, live trading blocked; run: sudo bash deploy/host-profile-refresh.sh"
         elif python3 -c 'import sys; sys.exit(0 if float(sys.argv[1]) > 12 else 1)' "${AGE_H}" 2>/dev/null; then warn "host profile file: ${AGE_H} h old — refresh overdue (timer runs every 6 h); check: systemctl status stoic-host-profile.timer"
         else ok "host profile file: ${AGE_H} h old (refreshed every 6 h, 24 h max)"; fi ;;
    esac
  else warn "host profile file: deploy/state/host_profile.json missing — readiness falls back to backend/.env (unverified once stale); run: sudo bash deploy/host-profile-refresh.sh"; fi
fi

hdr "configuration"
if [ -f .stoic-installed ]; then ok "installer: LOCKED since $(grep '^installed_at=' .stoic-installed | cut -d= -f2-) ($(grep '^mode=' .stoic-installed | cut -d= -f2-)$(lsattr .stoic-installed 2>/dev/null | grep -q '^....i' && echo ', immutable'))"
elif [ "$(envval backend/.env APP_ENV)" = production ]; then warn "installer: not locked — a re-run of bootstrap/install.sh would rebuild the stack (finish an install via deploy/bootstrap.sh to lock it)"; fi
for f in .env backend/.env; do [ -f "$f" ] && ok "$f present" || fail "$f missing (run deploy/install.sh)"; done
if [ -d secrets ]; then
  for s in mongo_url mongo_keyfile jwt_secret key_vault_master metrics_token order_auth_secret ledger_anchor_key bridge_token_hash_key signer_token signer_ed25519_key signer_cert.pem signer_cert_key.pem; do
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
  PUB_IP=$(curl -fs -m 5 https://api.ipify.org 2>/dev/null || echo "?"); DNS_IP=$(getent ahostsv4 "${DOMAIN}" 2>/dev/null | awk 'NR==1{print $1}')
  HOST_IPS=" $(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | tr '\n' ' ')${PUB_IP} "
  if [ -z "${DNS_IP}" ]; then fail "DNS: ${DOMAIN} does not resolve"
  elif case "${HOST_IPS}" in *" ${DNS_IP} "*) true ;; *) false ;; esac; then ok "DNS: ${DOMAIN} → ${DNS_IP} (this host)"
  elif [ "${PUB_IP}" != "?" ] && [ "${DNS_IP}" != "${PUB_IP}" ]; then warn "DNS: ${DOMAIN} → ${DNS_IP} but this host is ${PUB_IP} (host IPs:${HOST_IPS})"
  else ok "DNS: ${DOMAIN} → ${DNS_IP}"; fi
fi

hdr "docker mount propagation (why 'device or resource busy' on container removal)"
HOST_NS=$(readlink /proc/1/ns/mnt 2>/dev/null); DK_PID=$(pidof dockerd 2>/dev/null | awk '{print $1}')
if [ -n "${DK_PID}" ]; then
  DROOT=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker); DPROP=$(findmnt -no PROPAGATION "${DROOT}" 2>/dev/null || echo "?")
  case "${DPROP}" in
    *slave*) ok "${DROOT} propagation is '${DPROP}' — container mounts do not propagate into other namespaces" ;;
    *private*) warn "${DROOT} is private — dockerd flips a private root back to shared on restart; make it slave: mount --make-rslave ${DROOT} (bootstrap.sh persists this)" ;;
    *) fail "${DROOT} propagation is '${DPROP}' — every container rootfs mount is propagated into sandboxed services' namespaces and 'docker rm' hits EBUSY. Fix: mount --bind ${DROOT} ${DROOT}; mount --make-rslave ${DROOT} (bootstrap.sh persists this as a docker.service drop-in; dockerd flips a PRIVATE root back to shared, slave is kept)" ;;
  esac
  [ "$(readlink /proc/${DK_PID}/ns/mnt 2>/dev/null)" = "${HOST_NS}" ] && ok "dockerd runs in the host mount namespace" || warn "dockerd runs in its own mount namespace (systemd MountFlags?) — containerd/runc may not see its mounts"
  # cPanel VirtFS (jailed shells) bind-mounts /var/lib into /home/virtfs/<user>/ — a copy of every
  # container rootfs mount lands IN THE HOST NAMESPACE and shares the merged dir's dentry; once the
  # container is gone that copy alone makes `rmdir merged` → EBUSY (kernel is_local_mountpoint).
  ORPHANS=$(. deploy/lib.sh; orphan_merged_copies 2>/dev/null | grep -c . || true)
  if [ "${ORPHANS:-0}" -gt 0 ]; then
    fail "${ORPHANS} host-namespace copies of DEAD containers' rootfs mounts (cPanel VirtFS binds under /home/virtfs/*) — these alone cause 'device or resource busy'; fix: deploy/docker-orphan-mounts.sh --apply (Docker-only, no site is touched; update.sh now sweeps them before compose up)"
  else ok "no orphan container-rootfs mount copies in the host namespace (cPanel VirtFS)"; fi
  # foreign mount namespaces that hold copies of docker's overlay mounts: unmounting on the
  # host then leaves the copy → docker rm fails EBUSY. Name the processes so they can be fixed
  # (cPanel: CageFS/LVE, httpd PrivateTmp, imunify360, systemd units with PrivateTmp=yes)
  LEAKERS=$(for p in /proc/[0-9]*; do
      ns=$(readlink "$p/ns/mnt" 2>/dev/null) || continue; [ "${ns}" = "${HOST_NS}" ] && continue
      grep -qs '/var/lib/docker/overlay2/' "$p/mountinfo" || continue
      grep -qs 'docker\|containerd' "$p/cgroup" && continue      # container processes see their own rootfs — expected
      printf '%s\n' "$(cat "$p/comm" 2>/dev/null)"
    done | sort | uniq -c | sort -rn | head -8 | awk '{printf "%s×%s ", $2, $1}')
  [ -z "${LEAKERS// /}" ] && ok "no host process holds docker overlay mounts in a foreign mount namespace" \
    || warn "overlay mounts leaked into other mount namespaces (process×count): ${LEAKERS}— container removal hits EBUSY until these restart; fix: deploy/host-mount-fix.sh --apply --restart (MountFlags=slave drop-in per leaking unit)"
  # host processes with open files inside a container rootfs (scanners, indexers) → umount EBUSY
  HOLDERS=$(for c in $(docker compose ps -q 2>/dev/null); do
      m=$(docker inspect -f '{{.GraphDriver.Data.MergedDir}}' "$c" 2>/dev/null); [ -n "$m" ] || continue
      for pid in $(fuser -m "$m" 2>/dev/null); do
        grep -qs 'docker\|containerd' "/proc/$pid/cgroup" && continue
        printf '%s\n' "$(cat "/proc/$pid/comm" 2>/dev/null)"
      done
    done | sort | uniq -c | sort -rn | head -8 | awk '{printf "%s×%s ", $2, $1}')
  [ -z "${HOLDERS// /}" ] && ok "no host process has files open inside container rootfs mounts" \
    || warn "host processes hold files open inside container rootfs (process×count): ${HOLDERS}— exclude /var/lib/docker from scanners (imunify360/clamd/lfd/maldet) or stop them during upgrades"
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
  # RestartCount is cumulative for the container's lifetime (a frontend that
  # flapped while the backend was down during an earlier failed run keeps its
  # count) — a loop is only live if the count still grows: sample twice.
  restart_counts() { docker compose ps -q 2>/dev/null | xargs -r docker inspect --format '{{.Name}} {{.RestartCount}}' 2>/dev/null | sort; }
  R1=$(restart_counts); sleep "${RESTART_SAMPLE_GAP:-8}"; R2=$(restart_counts)
  LOOPING=$(comm -13 <(echo "${R1}") <(echo "${R2}") | awk '{print $1}' | tr '\n' ' ')
  HISTORIC=$(echo "${R2}" | awk '$2>3 {print $1"("$2")"}' | tr '\n' ' ')
  if [ -n "${LOOPING// /}" ]; then fail "restart loops (still restarting): ${LOOPING}"
  elif [ -n "${HISTORIC// /}" ]; then warn "restarted earlier but stable now: ${HISTORIC}(counts reset on recreate: docker compose up -d --force-recreate <svc>)"
  else ok "no crash-looping containers"; fi
else fail "docker compose project not found in $(pwd)"; fi

db_check

hdr "endpoints"
probe() { # probe <label> <url> [expect]
  local code err; rm -f /tmp/doctor_body
  code=$(curl -sS -m 15 -o /tmp/doctor_body -w '%{http_code}' "$2" 2>/tmp/doctor_err || true); [ -n "${code}" ] || code=000
  if [ "${code}" = "${3:-200}" ]; then ok "$1 → ${code}"
  else err=$(head -c 160 /tmp/doctor_body 2>/dev/null | tr '\n' ' '); [ -n "${err}" ] || err=$(sed -n 1p /tmp/doctor_err 2>/dev/null); fail "$1 → ${code} ${err}"; fi
}
probe "API /health"            http://127.0.0.1:8001/health
probe "API /api/health"        http://127.0.0.1:8001/api/health
probe "API /api/health/ready"  http://127.0.0.1:8001/api/health/ready
probe "frontend"               http://127.0.0.1:3000/
if [ -f /tmp/doctor_body ] && curl -s -m 15 http://127.0.0.1:8001/api/release-key -o /tmp/doctor_body; then
  grep -q '"mode": *"external"' /tmp/doctor_body && ok "release signer: external (sidecar)" || warn "release signer: $(grep -o '"mode": *"[a-z]*"' /tmp/doctor_body | head -1) — live signing not active"
fi
if [ -n "${DOMAIN}" ]; then
  for H in "${DOMAIN}" "www.${DOMAIN}"; do
  rm -f /tmp/doctor_body
  CODE=$(curl -sS -m 15 -o /tmp/doctor_body -w '%{http_code}' "https://${H}/api/health" 2>/tmp/doctor_err || true); [ -n "${CODE}" ] || CODE=000
  if [ "${CODE}" = 200 ]; then ok "https://${H}/api/health → 200"
  else
    ERR=$(sed -n 1p /tmp/doctor_err 2>/dev/null)
    KCODE=$(curl -sk -m 15 -o /dev/null -w '%{http_code}' "https://${H}/api/health" 2>/dev/null || true)
    if [ "${KCODE}" = 200 ]; then warn "https://${H}/api/health → proxy answers 200 but the TLS certificate is not valid for ${H} (${ERR:-curl ${CODE}}) — issue it (cPanel: SSL/TLS Status → Run AutoSSL) before going public"
    elif [ "${H}" != "${DOMAIN}" ] && [ "${CODE}" = 000 ] && ! getent ahostsv4 "${H}" >/dev/null 2>&1; then warn "https://${H}: no DNS record — add www as A → this host (or CNAME → ${DOMAIN}) if the www URL should work"
    else fail "https://${H}/api/health → ${CODE} ${ERR:-$(head -c 160 /tmp/doctor_body 2>/dev/null | tr '\n' ' ')}"; fi
  fi
  done
fi
if [ "$(envval .env CLOUDFLARE_MODE)" = true ]; then
  CF_END=$(openssl x509 -in secrets/origin_cert.pem -noout -enddate 2>/dev/null | cut -d= -f2)
  CF_DAYS=$(( ( $(date -d "${CF_END:-now}" +%s 2>/dev/null || date +%s) - $(date +%s) ) / 86400 ))
  [ "${CF_DAYS}" -gt 30 ] && ok "cloudflare: origin CA certificate valid ${CF_DAYS} more days$([ "$(envval .env CLOUDFLARE_ONLY)" = true ] && echo ' · cf-only (origin answers only to Cloudflare edges)')" \
    || fail "cloudflare: origin CA certificate expires in ${CF_DAYS} days (${CF_END:-unreadable}) — issue a new one and re-run bootstrap --cloudflare"
  ISS=$(openssl s_client -connect 127.0.0.1:443 -servername "${DOMAIN}" </dev/null 2>/dev/null | openssl x509 -noout -issuer 2>/dev/null)
  case "${ISS}" in *loud[fF]lare*) ok "cloudflare: Caddy serves the origin certificate on 443" ;; *) fail "cloudflare: 443 does not serve the Cloudflare origin certificate (issuer: ${ISS:-none})" ;; esac
fi
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
  docker compose ps > "${OUT}/compose_ps.txt" 2>&1
  # rendered compose config: Compose v2 INLINES env_file values into `environment:` —
  # redact every environment value (keys kept) so the bundle never carries a secret
  docker compose config --no-interpolate 2>/dev/null | python3 -c '
import re, sys
in_env = False; env_indent = -1
for line in sys.stdin:
    stripped = line.lstrip(); indent = len(line) - len(stripped)
    if re.match(r"environment:\s*$", stripped): in_env, env_indent = True, indent; sys.stdout.write(line); continue
    if in_env and indent <= env_indent and stripped.strip(): in_env = False
    if in_env and ":" in stripped and not stripped.startswith("-"):
        key = stripped.split(":", 1)[0]; sys.stdout.write(" " * indent + key + ": <redacted>\n"); continue
    if in_env and stripped.startswith("- ") and "=" in stripped:
        key = stripped[2:].split("=", 1)[0]; sys.stdout.write(" " * indent + "- " + key + "=<redacted>\n"); continue
    sys.stdout.write(line)
' > "${OUT}/compose_config.yml"
  for svc in backend signer mongo frontend worker-trading worker-protection worker-reconciliation worker-analytics worker-model worker-tuning caddy; do
    docker compose logs "${svc}" --tail 500 > "${OUT}/log_${svc}.txt" 2>/dev/null || true; done
  # env KEYS only — values are redacted
  for f in .env backend/.env; do [ -f "$f" ] && sed -E 's/^([A-Za-z_][A-Za-z0-9_]*)=.*/\1=<redacted>/' "$f" > "${OUT}/$(echo "$f" | tr / _).keys"; done
  ls -la secrets > "${OUT}/secrets_listing.txt" 2>/dev/null; command -v getenforce >/dev/null && getenforce > "${OUT}/selinux.txt"
  journalctl -u docker --no-pager -n 200 > "${OUT}/journal_docker.txt" 2>/dev/null || true
  ls /var/log/stoic-bootstrap-*.log >/dev/null 2>&1 && cp "$(ls -t /var/log/stoic-bootstrap-*.log | head -1)" "${OUT}/bootstrap.log"
  # belt and braces: refuse to ship the bundle if any known secret VALUE appears in it
  LEAK=0
  for f in .env backend/.env; do
    [ -f "$f" ] || continue
    while IFS= read -r val; do
      [ "${#val}" -ge 8 ] || continue
      grep -rqF -- "${val}" "${OUT}" 2>/dev/null && { LEAK=1; break; }
    done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=.+' "$f" | grep -viE '^(APP_ENV|DB_NAME|DOMAIN|COMPOSE_FILE|CORS_ORIGINS|MONGO_ROOT_USER|MONGO_APP_USER|REACT_APP_BACKEND_URL|DEPLOY_MODE|ATTESTATION_REQUIRED|TURNSTILE_EXPECTED_HOSTNAMES|RELEASE_SIGNER[A-Z_]*|RELEASE_PUBLIC_KEY_B64|ADMIN_EMAIL|SENDER_EMAIL|INSTALL_REPORT_EMAIL|HEALTHWATCH_EMAIL|HEALTHWATCH_REMIND_HOURS|STOIC_IMAGE_DIGEST|PRODUCTION_RETIRED_SECRETS|ADMIN_MFA_ENFORCED|CSRF_ENFORCE_ORIGIN)=' | cut -d= -f2-)
  done
  for sf in secrets/*; do [ -f "$sf" ] && grep -rqF -- "$(head -c 200 "$sf")" "${OUT}" 2>/dev/null && LEAK=1; done
  if [ "${LEAK}" = 1 ]; then
    rm -rf "${OUT}"; echo; echo "!! diagnostics bundle NOT written: a secret value was detected in the collected files (report this)"; exit 1
  fi
  tar -czf "${OUT}.tar.gz" -C diagnostics "$(basename "${OUT}")" && rm -rf "${OUT}"
  chmod 600 "${OUT}.tar.gz"
  echo; echo "diagnostics bundle: ${OUT}.tar.gz (env values redacted and leak-scanned — safe to share)"
fi

echo
echo "doctor: ${FAILS} FAIL · ${WARNS} WARN"
[ "${FAILS}" = 0 ]
