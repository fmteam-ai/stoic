#!/usr/bin/env bash
# STOIC update (re-publish): backup → pull target ref → rebuild WITH build
# provenance → restart → verify the COMPLETE trading topology (API, running
# build SHA, 6 workers, Mongo round trip, reconciliation lag, outbox backlog,
# schema compatibility, frontend), with automatic rollback to the previous
# ref if any verification fails.
#   deploy/update.sh             → latest origin/main
#   deploy/update.sh v1.4.2      → a tag
#   deploy/update.sh <sha>       → an exact commit
set -euo pipefail
cd "$(dirname "$0")/.."
. deploy/lib.sh

REF="${1:-origin/main}"
PREV=$(git rev-parse HEAD)
LOCK=/tmp/stoic-deploy.lock
exec 9>"${LOCK}"; flock -n 9 || { echo "ERROR: another deploy is running (${LOCK})"; exit 1; }

echo "== STOIC update: $(git rev-parse --short HEAD) -> ${REF} =="

echo "-- pre-update backup"
deploy/backup.sh backup

echo "-- fetching ${REF}"
git fetch --all --tags --prune
git checkout --detach "${REF}"
if [ "$(git rev-parse HEAD)" = "${PREV}" ]; then
  echo "   already on $(git rev-parse --short HEAD) — nothing to publish"
  exit 0
fi

rollback() {
  echo "!! verification failed — rolling back to ${PREV}"
  git checkout --detach "${PREV}"
  if [ "$(deploy_mode)" = "registry" ]; then verify_attestation >/dev/null 2>&1 || true; fi
  provision_images || true
  compose_up
  echo "!! rolled back to $(git rev-parse --short HEAD). Inspect: docker compose logs backend --tail 100"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD) auto-rollback-from=${REF}" >> deploy/releases.log
  exit 1
}

echo "-- release attestation gate (signed CI record: SHA · tests · scans · gates)"
verify_attestation || rollback

echo "-- provisioning images ($(deploy_mode): build with provenance | pull attested GHCR digests)"
provision_images || rollback

echo "-- restarting stack"
compose_up

echo "-- verifying API health"
wait_api_health 30 || rollback
echo "   API healthy"
verify_running_sha || rollback

echo "-- verifying frontend"
curl -fsS -o /dev/null http://127.0.0.1:3000 || rollback
echo "   frontend serving"

echo "-- verifying release readiness (workers, leases, Mongo, reconciliation, outbox, schema)"
BODY=$(wait_release_ready 45) || rollback
echo "   release-readiness: ${BODY}"

if grep -q 'docker-compose.forecast.yml' .env 2>/dev/null; then
  echo "-- verifying forecast profile"
  docker compose exec -T worker-trading python ops/verify_forecast_profile.py || rollback
fi

# round-5 P1 — topology policy gate: RECONCILE_EXPECT=accounts/enabled/bots in ./.env
# (e.g. 6/3/3). The signed read-only reconciliation must PASS on THIS database
# for THIS release or the deployment is rolled back. Evidence kept in release/evidence/.
RECONCILE_EXPECT=$(grep -E '^RECONCILE_EXPECT=' .env 2>/dev/null | cut -d= -f2-)
if [ -n "${RECONCILE_EXPECT}" ]; then
  echo "-- verifying production topology policy (${RECONCILE_EXPECT}, signed read-only reconciliation)"
  mkdir -p release/evidence
  if docker compose exec -T -e GIT_SHA="${GIT_SHA}" backend python ops/production_reconcile.py --expect "${RECONCILE_EXPECT}" \
       > "release/evidence/production-reconcile-$(git rev-parse --short HEAD).json" 2>/dev/null; then
    echo "   topology policy ${RECONCILE_EXPECT}: PASS (release/evidence/production-reconcile-$(git rev-parse --short HEAD).json)"
  else
    echo "!! topology policy ${RECONCILE_EXPECT} NOT met on this database — refusing this release"; rollback
  fi
fi

echo "-- pruning dangling images"
docker image prune -f >/dev/null 2>&1 || true

echo "   API + frontend + full topology verified on $(git rev-parse --short HEAD)"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD) update-from=$(git rev-parse --short "${PREV}")" >> deploy/releases.log
docker compose ps --format '{{.Name}}\t{{.Status}}'
echo "== update complete =="
