#!/usr/bin/env bash
# M119-6 — OFFLINE host migration ("lift-and-shift") for when the SOURCE API is down and the migration wizard
# (deploy/migrator, needs a healthy source API) cannot run. Three phases, two hosts:
#
#   SOURCE  deploy/migrate-offline.sh backup                 mongo-only backup (starts just the mongo container if needed)
#   SOURCE  deploy/migrate-offline.sh push <user@host> [dir] rsync -aHAX this install dir (code, .env, backend/.env, secrets/,
#                                                            backups/, deploy/state) to <dir> (default: same path) on the target;
#                                                            copies BACKUP_PASSPHRASE_FILE to the same path (0600). Refuses when
#                                                            the target dir already runs a stack.
#   TARGET  deploy/migrate-offline.sh restore [archive]      host prereqs → build images → mongo on a FRESH volume (users from
#                                                            mongo-init + rsynced secrets) → deploy/backup.sh restore (data+indexes,
#                                                            admin.* excluded — M119-4) → compose up (guarded) → verify
#                                                            (/api/health, workers, doctor --quiet) → cutover reminder.
#   options: --yes (no prompts) · --dry-run (print the commands, change nothing)
# Docker volumes are NOT copied: the data travels in the encrypted backup archive, nothing else.
set -euo pipefail
cd "$(dirname "$0")/.."
. deploy/lib.sh
. deploy/preflight.sh

PHASE="${1:-}"; shift || true
YES=0; DRY=0; ARGS=()
for a in "$@"; do case "$a" in --yes) YES=1 ;; --dry-run) DRY=1 ;; *) ARGS+=("$a") ;; esac; done
PREFLIGHT_YES="${YES}"; export PREFLIGHT_YES
run() { if [ "${DRY}" = 1 ]; then echo "   [dry-run] $*"; else "$@"; fi; }
confirm() { [ "${YES}" = 1 ] && return 0; [ "${DRY}" = 1 ] && return 0; [ -t 0 ] || { echo "!! no terminal to confirm — re-run with --yes"; return 1; }
            read -r -p "   $1 [y/N] " ans; [ "${ans}" = y ] || [ "${ans}" = Y ]; }
envv() { { grep -E "^$2=" "$1" 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d '"'"'"; }
latest_archive() { ls -t backups/stoic-mongo-*.archive.gz* 2>/dev/null | grep -v manifest | head -1 || true; }

case "${PHASE}" in
  backup)
    echo "== migrate-offline · SOURCE · backup"
    pass=$(envv .env BACKUP_PASSPHRASE_FILE); pass="${BACKUP_PASSPHRASE_FILE:-${pass}}"
    [ -n "${pass}" ] && [ -f "${pass}" ] || { echo "!! BACKUP_PASSPHRASE_FILE not set / missing — secrets/ cannot travel encrypted; set it in ./.env (outside ./secrets and ./backups)"; exit 1; }
    if ! docker compose ps --status running --services 2>/dev/null | grep -qx mongo; then
      echo "-- mongo container not running — starting ONLY mongo (the API stays down)"
      run docker compose up -d --no-deps mongo
      [ "${DRY}" = 1 ] || for _ in $(seq 1 30); do docker compose exec -T mongo mongosh --quiet --eval 'db.runCommand({ping:1}).ok' >/dev/null 2>&1 && break; sleep 2; done
    fi
    run bash deploy/backup.sh backup
    f=$(latest_archive)
    echo "   archive:  ${f:-<none>}"
    echo "   secrets:  $(ls backups/stoic-secrets-*.tar.gz.enc 2>/dev/null | tail -1 || echo '<none>')"
    echo "   next:     deploy/migrate-offline.sh push <user@new-host> [$(pwd)]" ;;

  push)
    dest="${ARGS[0]:?usage: deploy/migrate-offline.sh push <user@host> [dir]}"; dir="${ARGS[1]:-$(pwd)}"
    echo "== migrate-offline · SOURCE · push → ${dest}:${dir}"
    command -v rsync >/dev/null || { echo "!! rsync required on both hosts"; exit 1; }
    [ -n "$(latest_archive)" ] || { echo "!! no backup archive in backups/ — run: deploy/migrate-offline.sh backup"; exit 1; }
    if [ "${DRY}" != 1 ] && ssh -o BatchMode=yes "${dest}" "cd '${dir}' 2>/dev/null && docker compose ps -q 2>/dev/null | grep -q ." 2>/dev/null; then
      echo "!! ${dest}:${dir} already runs a compose stack — refusing to overwrite a live install (stop it or choose another dir)"; exit 1
    fi
    echo "   rsync -aHAX --delete (excluding node_modules, __pycache__, diagnostics/, frontend/build)"
    confirm "copy this install to ${dest}:${dir}?" || exit 1
    run ssh "${dest}" "mkdir -p '${dir}'"
    run rsync -aHAX --delete --numeric-ids --exclude node_modules --exclude __pycache__ --exclude diagnostics/ --exclude frontend/build \
        "$(pwd)/" "${dest}:${dir}/"
    pass=$(envv .env BACKUP_PASSPHRASE_FILE); pass="${BACKUP_PASSPHRASE_FILE:-${pass}}"
    if [ -n "${pass}" ] && [ -f "${pass}" ]; then
      echo "   copying BACKUP_PASSPHRASE_FILE → ${dest}:${pass} (0600)"
      run ssh "${dest}" "mkdir -p '$(dirname "${pass}")'"
      run rsync -a --chmod=600 "${pass}" "${dest}:${pass}"
    else
      echo "!! BACKUP_PASSPHRASE_FILE not found — copy it to the target by hand or the secrets archive cannot be decrypted"
    fi
    echo "   next, ON ${dest}:  cd ${dir} && sudo bash deploy/migrate-offline.sh restore --yes" ;;

  restore)
    echo "== migrate-offline · TARGET · restore"
    [ "${DRY}" = 1 ] || [ "$(id -u)" = 0 ] || { echo "!! run as root"; exit 1; }
    [ -f .env ] && [ -f backend/.env ] && [ -d secrets ] || { echo "!! .env / backend/.env / secrets/ missing — this is not a pushed install dir (run push on the source first)"; exit 1; }
    archive="${ARGS[0]:-$(latest_archive)}"
    [ -n "${archive}" ] && [ -f "${archive}" ] || { echo "!! no backup archive (backups/stoic-mongo-*.archive.gz[.enc]) — nothing to restore"; exit 1; }
    if [ "${DRY}" != 1 ] && docker compose ps -q 2>/dev/null | grep -q .; then
      echo "!! a compose stack already exists here — migrate-offline restores into a FRESH mongo volume only (docker compose down first if this is intended)"; exit 1
    fi
    for s in mongo_root_password mongo_app_password; do [ -s "secrets/${s}" ] || { echo "!! secrets/${s} missing — the mongo users cannot be created (rsync the source secrets/ first)"; exit 1; }; done
    echo "   archive: ${archive}"
    confirm "build images, create the mongo volume, restore ${archive} and start the stack here?" || exit 1
    echo "-- 1/6 host prerequisites (deploy/preflight.sh)"
    run preflight_host
    run virtfs_docker_root_gate
    echo "-- 2/6 building images with provenance"
    run provision_images
    echo "-- 3/6 mongo on a fresh volume (deploy/mongo-init.js creates root + app users from secrets/)"
    run docker compose up -d --no-deps mongo
    if [ "${DRY}" != 1 ]; then
      for _ in $(seq 1 45); do docker compose exec -T mongo sh -c 'mongosh --quiet -u "$MONGO_INITDB_ROOT_USERNAME" -p "$(cat /run/secrets/mongo_root_password)" --authenticationDatabase admin --eval "db.runCommand({ping:1}).ok"' >/dev/null 2>&1 && break; sleep 2; done
    fi
    echo "-- 4/6 restoring data + indexes (deploy/backup.sh restore — admin.* excluded, stack left stopped)"
    run env RESTORE_NO_START=1 bash deploy/backup.sh restore "${archive}"
    echo "-- 5/6 starting the stack"
    run compose_up_guarded || { rc=$?; [ "${rc}" = 2 ] && pause_trading_after_jam "migrate-offline"; exit "${rc}"; }
    if [ "${DRY}" != 1 ]; then
      wait_api_health 60 && echo "   /api/health: ok" || { echo "!! backend not healthy — docker compose logs backend --tail 80"; exit 1; }
      wait_workers_healthy 2>/dev/null && echo "   workers: healthy" || echo "!! workers not healthy yet — deploy/doctor.sh"
      record_host_profile || true
    fi
    echo "-- 6/6 verification"
    run bash deploy/doctor.sh --quiet || true
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD 2>/dev/null || echo '?') migrated-offline from=${archive}" >> deploy/releases.log
    cat <<EOF
== migrated. Before cutover:
   1. login + compare equity/trade counts with the backup date; readiness: make readiness (or /api/ops/release-readiness)
   2. the EA/agents still point at the OLD host — switch DNS/Cloudflare to this host (docs/HOST_MIGRATION.md → Cutover)
   3. on the OLD host keep the stack STOPPED (docker compose stop) until the fallback window is over, then wipe
   4. this host's RELEASE_PUBLIC_KEY_B64 / sidecar key came with the rsync — run: deploy/doctor.sh (SECURITY lines) and,
      if the sidecar key doubles as the release key, sudo bash deploy/rotate-runtime-key.sh
EOF
    ;;
  *) echo "usage: deploy/migrate-offline.sh backup | push <user@host> [dir] | restore [archive]   [--yes] [--dry-run]"; exit 64 ;;
esac
