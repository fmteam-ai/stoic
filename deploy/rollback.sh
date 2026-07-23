#!/usr/bin/env bash
# STOIC one-command rollback: pin the stack back to a previous known-good ref.
#   deploy/rollback.sh                  → roll back to the previous entry in deploy/releases.log
#   deploy/rollback.sh <git-ref>        → roll back to an explicit ref/tag
#   deploy/rollback.sh <ref> --with-db <archive>  → also restore the DB backup
# Verifies API health + full release readiness after the rollback and refuses
# to finish silently broken.
set -euo pipefail
cd "$(dirname "$0")/.."

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

echo "-- checking out ${REF}"
git fetch --all --tags --quiet || true
git checkout --detach "${REF}"

echo "-- rebuilding + restarting stack"
docker compose build
docker compose up -d

if [ -n "${WITH_DB}" ]; then
  echo "-- restoring database from ${WITH_DB}"
  deploy/backup.sh restore "${WITH_DB}"
fi

echo "-- verifying API health"
for i in $(seq 1 30); do
  curl -fsS http://localhost:8001/api/health >/dev/null 2>&1 && { OK=1; break; }
  sleep 2
done
[ "${OK:-0}" = 1 ] || { echo "!! API never became healthy after rollback — inspect: docker compose logs backend"; exit 1; }
echo "   API healthy"

echo "-- verifying full release readiness"
if [ -f secrets/metrics_token ]; then
  METRICS_TOKEN=$(cat secrets/metrics_token)
else
  METRICS_TOKEN=$(grep -E '^METRICS_TOKEN=' backend/.env | cut -d= -f2- | tr -d '"')
fi
for i in $(seq 1 45); do
  curl -fsS -H "X-Metrics-Token: ${METRICS_TOKEN}" \
    http://localhost:8001/api/ops/release-readiness >/dev/null 2>&1 && { READY=1; break; }
  sleep 4
done
if [ "${READY:-0}" != 1 ]; then
  echo "!! release-readiness not green after rollback — final state:"
  curl -sS -H "X-Metrics-Token: ${METRICS_TOKEN}" http://localhost:8001/api/ops/release-readiness || true
  exit 1
fi

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD) rollback-from=${CURRENT}" >> "${RELEASES_LOG}"
echo "== rollback to $(git rev-parse --short HEAD) complete and verified =="
