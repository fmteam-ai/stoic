#!/usr/bin/env bash
# STOIC one-command install: builds and starts the full stack (API + 5 workers
# + Mongo + frontend) with generated secrets. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== STOIC installer =="

command -v docker >/dev/null || { echo "ERROR: docker is required"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "ERROR: docker compose v2 is required"; exit 1; }

# 1 · backend/.env — create from template with generated secrets
if [ ! -f backend/.env ]; then
  echo "-- creating backend/.env from .env.example with generated secrets"
  cp backend/.env.example backend/.env
  gen() { python3 -c "import secrets;print(secrets.token_urlsafe(32))"; }
  for key in JWT_SECRET JWT_REFRESH_SECRET METRICS_TOKEN; do
    if grep -q "^${key}=$" backend/.env; then
      sed -i.bak "s|^${key}=$|${key}=$(gen)|" backend/.env
    fi
  done
  rm -f backend/.env.bak
  echo "   generated JWT/metrics secrets. Review backend/.env before go-live."
else
  echo "-- backend/.env exists — leaving untouched"
fi

# 2 · production guard reminder
if ! grep -q "^APP_ENV=production" backend/.env; then
  echo "   NOTE: set APP_ENV=production + CSRF_ENFORCE_ORIGIN=true + CORS_ORIGINS"
  echo "         in backend/.env before exposing this deployment publicly."
fi

# 3 · build + start
echo "-- building images"
docker compose build
echo "-- starting stack"
docker compose up -d

# 4 · health verification
echo "-- waiting for API health"
for i in $(seq 1 30); do
  if curl -fsS http://localhost:8001/api/health >/dev/null 2>&1 \
     || curl -fsS http://localhost:8001/api/ >/dev/null 2>&1; then
    echo "   API is up"
    break
  fi
  [ "$i" = 30 ] && { echo "ERROR: API did not become healthy"; docker compose logs backend | tail -30; exit 1; }
  sleep 2
done

echo "-- worker status"
docker compose ps --format '{{.Name}}\t{{.Status}}' | grep worker || true

echo ""
echo "== install complete =="
echo "   frontend:  http://localhost:3000"
echo "   API:       http://localhost:8001/api"
echo "   next:      deploy/backup.sh sets up backups; docs/DEPLOYMENT.md for the full checklist"
