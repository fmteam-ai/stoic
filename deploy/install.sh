#!/usr/bin/env bash
# STOIC one-command install: builds and starts the full stack (API + 6 workers
# + Mongo + frontend) with generated secrets. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== STOIC installer =="

command -v docker >/dev/null || { echo "ERROR: docker is required"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "ERROR: docker compose v2 is required"; exit 1; }

gen() { python3 -c "import secrets;print(secrets.token_urlsafe(32))"; }

# 0 · Docker secrets — all credentials live here, never in .env files
if [ ! -d secrets ]; then
  echo "-- generating Docker secrets in ./secrets/"
  mkdir -p secrets && chmod 700 secrets
  DB_NAME_VAL=ai_trading_bot
  APP_PWD=$(gen)
  gen > secrets/mongo_root_password
  printf '%s' "${APP_PWD}" > secrets/mongo_app_password
  printf 'mongodb://stoic_app:%s@mongo:27017/%s?authSource=%s' \
    "${APP_PWD}" "${DB_NAME_VAL}" "${DB_NAME_VAL}" > secrets/mongo_url
  gen > secrets/jwt_secret
  gen > secrets/key_vault_master
  gen > secrets/metrics_token
  chmod 600 secrets/*
  echo "   generated mongo_root_password, mongo_app_password, mongo_url,"
  echo "   jwt_secret, key_vault_master, metrics_token (mode 600)."
else
  echo "-- ./secrets exists — leaving untouched"
fi

# 1 · compose-level .env — NON-SECRET config only
if [ ! -f .env ]; then
  echo "-- creating ./.env (compose config) from .env.example"
  cp .env.example .env
  sed -i.bak \
    -e "s|^MONGO_ROOT_USER=$|MONGO_ROOT_USER=stoic_root|" \
    -e "s|^MONGO_APP_USER=$|MONGO_APP_USER=stoic_app|" \
    -e "s|^DB_NAME=$|DB_NAME=ai_trading_bot|" \
    .env
  rm -f .env.bak
else
  echo "-- ./.env exists — leaving untouched"
fi

# 1 · backend/.env — create from template (non-secret config; credentials
#     come from Docker secrets via *_FILE indirection)
if [ ! -f backend/.env ]; then
  echo "-- creating backend/.env from .env.example"
  cp backend/.env.example backend/.env
  sed -i.bak "s|^DB_NAME=$|DB_NAME=ai_trading_bot|" backend/.env
  rm -f backend/.env.bak
  echo "   review backend/.env for integration keys before go-live."
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
