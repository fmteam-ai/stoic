#!/usr/bin/env bash
# STOIC Mongo backup/restore.
#   deploy/backup.sh backup            → timestamped archive in ./backups (14-day retention)
#   deploy/backup.sh restore <file>    → maintenance-mode restore (validates first, stops writes)
#   deploy/backup.sh schedule          → print a crontab line for nightly backups
set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
MONGO_SVC="${MONGO_SVC:-mongo}"
APP_SERVICES="backend worker-trading worker-protection worker-reconciliation worker-analytics worker-model worker-tuning"

# mongodump/mongorestore run inside the mongo container as root
# (MONGO_INITDB_ROOT_* are in the container environment).
MONGO_AUTH='-u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin'

case "${1:-backup}" in
  backup)
    mkdir -p "${BACKUP_DIR}"
    STAMP=$(date -u +%Y%m%d-%H%M%S)
    OUT="${BACKUP_DIR}/stoic-mongo-${STAMP}.archive.gz"
    echo "-- dumping MongoDB to ${OUT}"
    docker compose exec -T "${MONGO_SVC}" sh -c "mongodump ${MONGO_AUTH} --archive --gzip" > "${OUT}"
    echo "   $(du -h "${OUT}" | cut -f1) written"
    echo "-- pruning archives older than ${RETENTION_DAYS} days"
    find "${BACKUP_DIR}" -name "stoic-mongo-*.archive.gz" -mtime +"${RETENTION_DAYS}" -delete
    ls -lh "${BACKUP_DIR}" | tail -5
    ;;
  restore)
    FILE="${2:?usage: deploy/backup.sh restore <archive.gz>}"
    [ -f "${FILE}" ] || { echo "ERROR: ${FILE} not found"; exit 1; }
    echo "!! DESTRUCTIVE restore from ${FILE} in 5s — Ctrl-C to abort"
    sleep 5

    echo "-- 1/5 entering maintenance mode: stopping API + all workers (no writes during restore)"
    docker compose stop ${APP_SERVICES}

    echo "-- 2/5 validating archive (dry run — no data touched)"
    if ! docker compose exec -T "${MONGO_SVC}" sh -c "mongorestore ${MONGO_AUTH} --archive --gzip --dryRun" < "${FILE}"; then
      echo "ERROR: archive failed validation — NOT restoring. Stack left stopped;"
      echo "       restart with: docker compose up -d"
      exit 1
    fi

    echo "-- 3/5 restoring (drop + replace)"
    docker compose exec -T "${MONGO_SVC}" sh -c "mongorestore ${MONGO_AUTH} --archive --gzip --drop" < "${FILE}"

    echo "-- 4/5 starting API only (workers stay stopped until reconciliation is verified)"
    docker compose start backend

    echo "-- 5/5 REQUIRED before trading resumes:"
    echo "   1. Verify the restore: login, check equity/trade counts against the backup date."
    echo "   2. Reconcile with the broker: EA heartbeat + position adoption must match"
    echo "      broker truth (Bot Health → Execution Health, all green)."
    echo "   3. Check readiness:  curl -H \"X-Metrics-Token: \$(grep ^METRICS_TOKEN= backend/.env | cut -d= -f2-)\" http://localhost:8001/api/ops/release-readiness"
    echo "   4. Only then resume workers:  docker compose up -d"
    ;;
  schedule)
    echo "Add to crontab (crontab -e) for nightly 02:15 UTC backups:"
    echo "15 2 * * * cd $(pwd) && deploy/backup.sh backup >> ${BACKUP_DIR}/backup.log 2>&1"
    ;;
  *)
    echo "usage: deploy/backup.sh [backup|restore <file>|schedule]"; exit 1 ;;
esac
