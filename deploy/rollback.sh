#!/usr/bin/env bash
# STOIC one-command rollback: pin the stack back to a previous known-good ref.
#   deploy/rollback.sh                  → roll back to the previous entry in deploy/releases.log
#   deploy/rollback.sh <git-ref>        → roll back to an explicit ref/tag
#   deploy/rollback.sh <ref> --with-db <archive>  → also restore the DB backup
# Verifies API health + full release readiness after the rollback and refuses
# to finish silently broken.
set -euo pipefail
cd "$(dirname "$0")/.."
. deploy/lib.sh

RELEASES_LOG="deploy/releases.log"
REF="${1:-}"
WITH_DB=""
if [ "${2:-}" = "--with-db" ]; then WITH_DB="${3:?--with-db needs an archive path}"; fi

if [ -z "${REF}" ]; then
  [ -f "${RELEASES_LOG}" ] || { echo "ERROR: no ref given and ${RELEASES_LOG} missing"; exit 1; }
  # last line = current deploy, second-to-last = previous known-good
  REF=$(tail -2 "${RELEASES_LOG}" | head -1 | awk '{print $2}')
  [ -n "${REF}" ] || { echo "ERROR: could not determine previous release from ${RELEASES_LOG}"; exit 1; }
  echo "-- rolling back to previous release: ${REF}"
fi

CURRENT=$(git rev-parse --short HEAD)
echo "== STOIC rollback: ${CURRENT} -> ${REF} =="

echo "-- safety backup of the CURRENT database state"
deploy/backup.sh backup

# Q-3 — order: stop → restore DATA → old CODE → start. The database is restored while the
# stack is stopped and BEFORE the old code comes up, so neither release ever runs on the
# other one's data.
if [ -n "${WITH_DB}" ]; then
  echo "-- restoring database from ${WITH_DB} (stack stopped, no restart)"
  RESTORE_NO_START=1 deploy/backup.sh restore "${WITH_DB}" || { echo "ERROR: database restore failed — stack left stopped; nothing else changed"; exit 1; }
fi

echo "-- checking out ${REF}"
git fetch --all --tags --quiet || true
git checkout --detach "${REF}"

echo "-- provisioning images ($(deploy_mode)) + restarting stack"
if [ "$(deploy_mode)" = "registry" ]; then
  verify_attestation || { echo "ERROR: ${REF} is not an attested release — registry rollback needs its digests"; exit 1; }
fi
provision_images || { echo "ERROR: image provisioning failed during rollback"; exit 1; }
compose_up

echo "-- verifying API health"
wait_api_health 30 || { echo "!! API never became healthy after rollback — inspect: docker compose logs backend"; exit 1; }
echo "   API healthy"
verify_running_sha || exit 1

echo "-- verifying release readiness (same policy as install/update: infra checks; release gates block only in production)"
wait_release_ready 45 >/dev/null || { echo "!! release-readiness not acceptable after rollback (see failing checks above)"; exit 1; }
echo "   release readiness acceptable"

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD) rollback-from=${CURRENT}" >> "${RELEASES_LOG}"
echo "== rollback to $(git rev-parse --short HEAD) complete and verified =="
