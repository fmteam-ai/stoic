#!/usr/bin/env bash
# STOIC update: backup → pull target ref → rebuild → restart → verify,
# with automatic rollback to the previous ref if the health check fails.
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
  echo "!! health check failed — rolling back to ${PREV}"
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

echo "-- verifying health"
for i in $(seq 1 30); do
  if curl -fsS http://localhost:8001/api/health >/dev/null 2>&1 \
     || curl -fsS http://localhost:8001/api/ >/dev/null 2>&1; then
    echo "   API healthy on $(git rev-parse --short HEAD)"
    docker compose ps --format '{{.Name}}\t{{.Status}}'
    echo "== update complete =="
    exit 0
  fi
  sleep 2
done
rollback
