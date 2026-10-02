#!/usr/bin/env bash
# STOIC Mongo backup/restore/verify/offsite.
#   deploy/backup.sh backup            → timestamped archive in ./backups (14-day retention)
#                                        + per-collection count manifest
#                                        + AES-256 encryption when BACKUP_PASSPHRASE_FILE is set
#                                        + auto off-site push when BACKUP_OFFSITE=true
#   deploy/backup.sh restore <file>    → maintenance-mode restore (validates first, stops writes)
#   deploy/backup.sh verify  <file>    → AUTOMATED RESTORE VERIFICATION: restores the archive
#                                        into a throwaway Mongo container and compares every
#                                        collection count against the manifest (PASS/FAIL)
#   deploy/backup.sh offsite <file>    → push archive+manifest off-site via rclone
#                                        (BACKUP_RCLONE_REMOTE) or aws s3 (BACKUP_S3_URI)
#   deploy/backup.sh schedule          → print crontab lines for nightly backup + verify
set -euo pipefail
case ":${PATH}:" in *":/usr/local/lib/stoic/bin:"*) ;; *) export PATH="/usr/local/lib/stoic/bin:${PATH}" ;; esac   # private python >= 3.9 (bootstrap.sh)
cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
MONGO_SVC="${MONGO_SVC:-mongo}"
APP_SERVICES="backend worker-trading worker-protection worker-reconciliation worker-analytics worker-model worker-tuning"

# mongodump/mongorestore run inside the mongo container as root. The root
# password arrives via env OR Docker secret file — the inner shell resolves
# whichever is present.
MONGO_AUTH='-u "$MONGO_INITDB_ROOT_USERNAME" -p "${MONGO_INITDB_ROOT_PASSWORD:-$(cat "$MONGO_INITDB_ROOT_PASSWORD_FILE")}" --authenticationDatabase admin'

_encrypt_args() {
  # openssl args shared by encrypt + decrypt (AES-256-CBC, PBKDF2 200k iters)
  echo "enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass file:${BACKUP_PASSPHRASE_FILE}"
}

_decrypt_to() {
  # $1=encrypted file  $2=output plaintext path
  [ -n "${BACKUP_PASSPHRASE_FILE:-}" ] && [ -f "${BACKUP_PASSPHRASE_FILE}" ] \
    || { echo "ERROR: ${1} is encrypted — set BACKUP_PASSPHRASE_FILE"; exit 1; }
  # shellcheck disable=SC2046
  openssl $(_encrypt_args) -d -in "$1" -out "$2"
}

_write_manifest() {
  # $1 = archive path — writes $1.manifest.json with per-collection counts
  docker compose exec -T "${MONGO_SVC}" sh -c "mongosh ${MONGO_AUTH} --quiet --eval '
    const dbs = db.getSiblingDB(\"admin\").adminCommand({listDatabases:1}).databases
      .map(d => d.name).filter(n => ![\"admin\",\"local\",\"config\"].includes(n));
    const out = {};
    for (const n of dbs) {
      const d = db.getSiblingDB(n); out[n] = {};
      for (const c of d.getCollectionNames()) out[n][c] = d.getCollection(c).countDocuments();
    }
    print(JSON.stringify({created_at: new Date().toISOString(), databases: out}));
  '" > "$1.manifest.json"
  echo "   manifest: $(python3 -c "import json;d=json.load(open('$1.manifest.json'));print(sum(len(v) for v in d['databases'].values()), 'collections')" 2>/dev/null || echo written)"
}

case "${1:-backup}" in
  backup)
    mkdir -p "${BACKUP_DIR}"
    STAMP=$(date -u +%Y%m%d-%H%M%S)
    OUT="${BACKUP_DIR}/stoic-mongo-${STAMP}.archive.gz"
    echo "-- dumping MongoDB to ${OUT}"
    docker compose exec -T "${MONGO_SVC}" sh -c "mongodump ${MONGO_AUTH} --archive --gzip" > "${OUT}"
    echo "   $(du -h "${OUT}" | cut -f1) written"
    _write_manifest "${OUT}"
    if [ -n "${BACKUP_PASSPHRASE_FILE:-}" ] && [ -f "${BACKUP_PASSPHRASE_FILE}" ]; then
      echo "-- encrypting (AES-256-CBC, PBKDF2)"
      # shellcheck disable=SC2046
      openssl $(_encrypt_args) -in "${OUT}" -out "${OUT}.enc"
      rm -f "${OUT}"
      OUT="${OUT}.enc"
      echo "   encrypted: ${OUT}"
    fi
    echo "-- pruning archives older than ${RETENTION_DAYS} days"
    find "${BACKUP_DIR}" \( -name "stoic-mongo-*.archive.gz" -o -name "stoic-mongo-*.archive.gz.enc" -o -name "stoic-mongo-*.manifest.json" \) -mtime +"${RETENTION_DAYS}" -delete
    if [ "${BACKUP_OFFSITE:-false}" = "true" ]; then
      "$0" offsite "${OUT}"
    fi
    ls -lh "${BACKUP_DIR}" | tail -5
    ;;

  restore)
    FILE="${2:?usage: deploy/backup.sh restore <archive.gz[.enc]>}"
    [ -f "${FILE}" ] || { echo "ERROR: ${FILE} not found"; exit 1; }
    PLAIN="${FILE}"
    case "${FILE}" in *.enc)
      PLAIN="$(mktemp /tmp/stoic-restore-XXXXXX.archive.gz)"
      trap 'rm -f "${PLAIN}"' EXIT
      echo "-- decrypting archive"
      _decrypt_to "${FILE}" "${PLAIN}" ;;
    esac
    echo "!! DESTRUCTIVE restore from ${FILE} in 5s — Ctrl-C to abort"
    sleep 5

    echo "-- 1/5 entering maintenance mode: stopping API + all workers (no writes during restore)"
    docker compose stop ${APP_SERVICES}

    echo "-- 2/5 validating archive (dry run — no data touched)"
    if ! docker compose exec -T "${MONGO_SVC}" sh -c "mongorestore ${MONGO_AUTH} --archive --gzip --dryRun" < "${PLAIN}"; then
      echo "ERROR: archive failed validation — NOT restoring. Stack left stopped;"
      echo "       restart with: docker compose up -d"
      exit 1
    fi

    echo "-- 3/5 restoring (drop + replace)"
    docker compose exec -T "${MONGO_SVC}" sh -c "mongorestore ${MONGO_AUTH} --archive --gzip --drop" < "${PLAIN}"

    echo "-- 4/5 starting API only (workers stay stopped until reconciliation is verified)"
    docker compose start backend

    echo "-- 5/5 REQUIRED before trading resumes:"
    echo "   1. Verify the restore: login, check equity/trade counts against the backup date."
    echo "   2. Reconcile with the broker: EA heartbeat + position adoption must match"
    echo "      broker truth (Bot Health → Execution Health, all green)."
    echo "   3. Check readiness:  curl -H \"X-Metrics-Token: \$(grep ^METRICS_TOKEN= backend/.env | cut -d= -f2-)\" http://localhost:8001/api/ops/release-readiness"
    echo "   4. Only then resume workers:  docker compose up -d"
    ;;

  verify)
    FILE="${2:-$(ls -t "${BACKUP_DIR}"/stoic-mongo-*.archive.gz* 2>/dev/null | grep -v manifest | sed -n 1p)}"
    [ -n "${FILE}" ] && [ -f "${FILE}" ] || { echo "ERROR: no archive to verify"; exit 1; }
    MANIFEST="${FILE%.enc}.manifest.json"
    [ -f "${MANIFEST}" ] || { echo "ERROR: manifest ${MANIFEST} not found"; exit 1; }
    PLAIN="${FILE}"
    case "${FILE}" in *.enc)
      PLAIN="$(mktemp /tmp/stoic-verify-XXXXXX.archive.gz)"
      echo "-- decrypting archive for verification"
      _decrypt_to "${FILE}" "${PLAIN}" ;;
    esac
    SCRATCH="stoic-restore-verify-$$"
    cleanup() { docker rm -f "${SCRATCH}" >/dev/null 2>&1 || true; [ "${PLAIN}" != "${FILE}" ] && rm -f "${PLAIN}"; }
    trap cleanup EXIT
    echo "-- starting throwaway Mongo container (${SCRATCH})"
    docker run -d --rm --name "${SCRATCH}" mongo:7 >/dev/null
    for i in $(seq 1 30); do
      docker exec "${SCRATCH}" mongosh --quiet --eval 'db.runCommand({ping:1})' >/dev/null 2>&1 && break
      sleep 2
    done
    echo "-- restoring archive into scratch instance"
    docker exec -i "${SCRATCH}" mongorestore --archive --gzip --quiet < "${PLAIN}"
    echo "-- comparing collection counts against manifest"
    ACTUAL=$(docker exec "${SCRATCH}" mongosh --quiet --eval '
      const dbs = db.getSiblingDB("admin").adminCommand({listDatabases:1}).databases
        .map(d => d.name).filter(n => !["admin","local","config"].includes(n));
      const out = {};
      for (const n of dbs) {
        const d = db.getSiblingDB(n); out[n] = {};
        for (const c of d.getCollectionNames()) out[n][c] = d.getCollection(c).countDocuments();
      }
      print(JSON.stringify(out));')
    python3 - "$MANIFEST" <<EOF
import json, sys
expected = json.load(open(sys.argv[1]))["databases"]
actual = json.loads('''${ACTUAL}''')
bad = []
for dbn, cols in expected.items():
    for col, n in cols.items():
        got = actual.get(dbn, {}).get(col)
        if got != n:
            bad.append(f"{dbn}.{col}: expected {n}, restored {got}")
if bad:
    print("RESTORE VERIFICATION FAILED:")
    [print("  -", b) for b in bad]
    sys.exit(1)
total = sum(len(c) for c in expected.values())
print(f"RESTORE VERIFICATION PASSED — {total} collections match the manifest exactly.")
EOF
    ;;

  offsite)
    FILE="${2:-$(ls -t "${BACKUP_DIR}"/stoic-mongo-*.archive.gz* 2>/dev/null | grep -v manifest | sed -n 1p)}"
    [ -n "${FILE}" ] && [ -f "${FILE}" ] || { echo "ERROR: no archive to push"; exit 1; }
    MANIFEST="${FILE%.enc}.manifest.json"
    if [ -n "${BACKUP_RCLONE_REMOTE:-}" ]; then
      echo "-- pushing to rclone remote ${BACKUP_RCLONE_REMOTE}"
      rclone copyto "${FILE}" "${BACKUP_RCLONE_REMOTE}/$(basename "${FILE}")"
      [ -f "${MANIFEST}" ] && rclone copyto "${MANIFEST}" "${BACKUP_RCLONE_REMOTE}/$(basename "${MANIFEST}")"
      echo "   off-site push complete (rclone)"
    elif [ -n "${BACKUP_S3_URI:-}" ]; then
      echo "-- pushing to ${BACKUP_S3_URI}"
      aws s3 cp "${FILE}" "${BACKUP_S3_URI}/$(basename "${FILE}")"
      [ -f "${MANIFEST}" ] && aws s3 cp "${MANIFEST}" "${BACKUP_S3_URI}/$(basename "${MANIFEST}")"
      echo "   off-site push complete (s3)"
    else
      echo "ERROR: set BACKUP_RCLONE_REMOTE (e.g. remote:stoic-backups) or BACKUP_S3_URI (e.g. s3://bucket/stoic)"
      exit 1
    fi
    ;;

  schedule)
    echo "Add to crontab (crontab -e) for nightly 02:15 UTC backups + 03:00 restore verification:"
    echo "15 2 * * * cd $(pwd) && deploy/backup.sh backup >> ${BACKUP_DIR}/backup.log 2>&1"
    echo "0 3 * * * cd $(pwd) && deploy/backup.sh verify >> ${BACKUP_DIR}/verify.log 2>&1"
    ;;

  *)
    echo "usage: deploy/backup.sh [backup|restore <file>|verify [file]|offsite [file]|schedule]"; exit 1 ;;
esac
