#!/usr/bin/env bash
# STOIC Mongo backup/restore.
#   deploy/backup.sh backup            → timestamped archive in ./backups (14-day retention)
#   deploy/backup.sh restore <file>    → restore an archive (DESTRUCTIVE)
#   deploy/backup.sh schedule          → print a crontab line for nightly backups
set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
MONGO_SVC="${MONGO_SVC:-mongo}"

case "${1:-backup}" in
  backup)
    mkdir -p "${BACKUP_DIR}"
    STAMP=$(date -u +%Y%m%d-%H%M%S)
    OUT="${BACKUP_DIR}/stoic-mongo-${STAMP}.archive.gz"
    echo "-- dumping MongoDB to ${OUT}"
    docker compose exec -T "${MONGO_SVC}" mongodump --archive --gzip > "${OUT}"
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
    docker compose exec -T "${MONGO_SVC}" mongorestore --archive --gzip --drop < "${FILE}"
    echo "-- restore complete. Restart services: docker compose restart backend"
    ;;
  schedule)
    echo "Add to crontab (crontab -e) for nightly 02:15 UTC backups:"
    echo "15 2 * * * cd $(pwd) && deploy/backup.sh backup >> ${BACKUP_DIR}/backup.log 2>&1"
    ;;
  *)
    echo "usage: deploy/backup.sh [backup|restore <file>|schedule]"; exit 1 ;;
esac
