#!/usr/bin/env bash
# STOIC restart — the supported way to apply a backend/.env change (or bounce app services) on a
# running install. Editing .env and running a plain `docker compose up -d` is NOT supported on
# cPanel/RHEL-8 hosts: without the preflight the recreate jams on overlay EBUSY ("driver overlay2
# failed to remove root filesystem … device or resource busy"), compose leaves "<id>_stoic-*"
# containers behind and the API stays offline until a reboot.
#
#   sudo bash deploy/restart.sh --env-changed          preflight → recreate backend + 7 workers with the new env
#   sudo bash deploy/restart.sh --env-changed --all    …also frontend + signer (mongo is never recreated here)
#   sudo bash deploy/restart.sh backend worker-model   recreate the named services (same preflight)
#   options: --no-host-changes (refuse instead of applying missing host prerequisites) · --yes (no prompts)
#   make apply-env  =  deploy/restart.sh --env-changed --yes
set -euo pipefail
cd "$(dirname "$0")/.."
. deploy/lib.sh
. deploy/preflight.sh

ENV_CHANGED=0; ALL=0; SERVICES=()
for a in "$@"; do
  case "$a" in
    --env-changed) ENV_CHANGED=1 ;;
    --all) ALL=1 ;;
    --no-host-changes) PREFLIGHT_NO_HOST_CHANGES=1 ;;
    --yes|-y) PREFLIGHT_YES=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    -*) echo "unknown option: $a"; exit 64 ;;
    *) SERVICES+=("$a") ;;
  esac
done
if [ "${ENV_CHANGED}" = 1 ] && [ "${#SERVICES[@]}" = 0 ]; then
  SERVICES=(backend worker-trading worker-protection worker-reconciliation worker-analytics worker-security worker-model worker-tuning)
  [ "${ALL}" = 1 ] && SERVICES+=(frontend signer)
fi
[ "${#SERVICES[@]}" -gt 0 ] || [ "${ALL}" = 1 ] || { echo "usage: deploy/restart.sh --env-changed [--all] | <service…> [--no-host-changes] [--yes]"; exit 64; }

LOCK=/tmp/stoic-deploy.lock
exec 9>"${LOCK}"; flock -n 9 || { echo "ERROR: another deploy is running (${LOCK})"; exit 1; }
echo "== STOIC restart$( [ "${ENV_CHANGED}" = 1 ] && echo ' (--env-changed)') on $(git rev-parse --short HEAD 2>/dev/null || echo '?') =="

preflight_host || exit 1
clean_leftovers || { echo "!! leftovers could not be removed — see above"; exit 1; }
if [ "${ENV_CHANGED}" = 1 ]; then ensure_release_public_key_pin; record_host_profile; fi

if [ "${#SERVICES[@]}" -gt 0 ]; then
  echo "-- recreating ${#SERVICES[@]} service(s) with the current backend/.env: ${SERVICES[*]}"
  compose_up_guarded --force-recreate --no-deps "${SERVICES[@]}" || exit 1
else
  echo "-- recreating the whole stack"
  compose_up_guarded --force-recreate || exit 1
fi

echo "-- verifying API health"
wait_api_health 30 || { echo "!! API not healthy after the restart — docker compose logs backend --tail 100"; exit 1; }
echo "   API healthy"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD 2>/dev/null || echo '?') restart$( [ "${ENV_CHANGED}" = 1 ] && echo ' --env-changed') ${SERVICES[*]:-all}" >> deploy/releases.log
docker compose ps --format 'table {{.Name}}\t{{.Status}}'
echo "== restart complete =="
