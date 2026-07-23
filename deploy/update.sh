#!/usr/bin/env bash
# STOIC update: backup → pull target ref → rebuild → restart → verify the
# COMPLETE trading topology (API, 6 workers, Mongo round trip, reconciliation
# lag, outbox backlog, schema compatibility, frontend), with automatic
# rollback to the previous ref if any verification fails.
# Usage: deploy/update.sh [git-ref]   (default: latest origin/main)
set -euo pipefail
cd "$(dirname "$0")/.."

REF="${1:-origin/main}"
PREV=$(git rev-parse HEAD)

echo "== STOIC update: $(git rev-parse --short HEAD) -> ${REF} =="

echo "-- pre-update backup"
deploy/backup.sh backup

echo "-- fetching ${REF}"
git fetch --all --tags
git checkout --detach "${REF}"

rollback() {
  echo "!! verification failed — rolling back to ${PREV}"
  git checkout --detach "${PREV}"
  docker compose build
  docker compose up -d
  echo "!! rolled back. Inspect logs: docker compose logs backend"
  exit 1
}

echo "-- rebuilding images"
docker compose build

echo "-- restarting stack"
docker compose up -d

echo "-- verifying API health"
API_OK=0
for i in $(seq 1 30); do
  if curl -fsS http://localhost:8001/api/health >/dev/null 2>&1; then
    API_OK=1; break
  fi
  sleep 2
done
[ "$API_OK" = 1 ] || rollback
echo "   API healthy"

echo "-- verifying frontend"
curl -fsS -o /dev/null http://localhost:3000 || rollback
echo "   frontend serving"

echo "-- verifying release readiness (workers, leases, Mongo, reconciliation, outbox, schema)"
if [ -f secrets/metrics_token ]; then
  METRICS_TOKEN=$(cat secrets/metrics_token)
else
  METRICS_TOKEN=$(grep -E '^METRICS_TOKEN=' backend/.env | cut -d= -f2- | tr -d '"')
fi
[ -n "${METRICS_TOKEN}" ] || { echo "ERROR: metrics token missing (secrets/metrics_token or backend/.env)"; rollback; }
READY=0
for i in $(seq 1 45); do   # workers need time to acquire leases (~45s lease TTL)
  BODY=$(curl -fsS -H "X-Metrics-Token: ${METRICS_TOKEN}" \
         http://localhost:8001/api/ops/release-readiness 2>/dev/null) && READY=1 && break
  sleep 4
done
if [ "$READY" = 1 ]; then
  echo "   release-readiness: ${BODY}"
else
  echo "!! release-readiness never returned ready:"
  curl -sS -H "X-Metrics-Token: ${METRICS_TOKEN}" \
    http://localhost:8001/api/ops/release-readiness || true
  rollback
fi

echo "   API + frontend + full topology verified on $(git rev-parse --short HEAD)"
docker compose ps --format '{{.Name}}\t{{.Status}}'
echo "== update complete =="
