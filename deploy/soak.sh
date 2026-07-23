#!/usr/bin/env bash
# STOIC 2-week soak-test kit (docs/MT5_VALIDATION_CAMPAIGN.md §7).
#   deploy/soak.sh start            → record soak start + print the drill schedule
#   deploy/soak.sh status           → elapsed time, release-readiness, exit criteria
#   deploy/soak.sh drill alert      → kill worker-trading, verify lease failover, restart
#   deploy/soak.sh drill recovery   → nightly-backup validation drill (dry-run restore)
#   deploy/soak.sh drill panic      → print the manual panic-drill procedure
#   deploy/soak.sh log "<note>"     → append a timestamped entry to the soak log
set -euo pipefail
cd "$(dirname "$0")/.."

STATE_DIR="./soak"
LOG_FILE="docs/campaigns/SOAK_LOG.md"

metrics_token() {
  if [ -f secrets/metrics_token ]; then cat secrets/metrics_token
  else grep -E '^METRICS_TOKEN=' backend/.env | cut -d= -f2- | tr -d '"'; fi
}

readiness() {
  curl -sS -H "X-Metrics-Token: $(metrics_token)" \
    http://localhost:8001/api/ops/release-readiness
}

log_entry() {
  printf -- '- `%s` — %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "${LOG_FILE}"
  echo "   logged to ${LOG_FILE}"
}

case "${1:-status}" in
  start)
    mkdir -p "${STATE_DIR}"
    date -u +%Y-%m-%dT%H:%M:%SZ > "${STATE_DIR}/started_at"
    git rev-parse HEAD > "${STATE_DIR}/release_commit"
    log_entry "SOAK STARTED on $(git describe --tags --always) ($(git rev-parse --short HEAD))"
    echo "== 2-week soak started =="
    echo "   release: $(git describe --tags --always)"
    echo "   Weekly drill schedule (run each at least once per week):"
    echo "     deploy/soak.sh drill alert      (kill worker → alert + lease failover)"
    echo "     deploy/soak.sh drill recovery   (backup restore validation)"
    echo "     deploy/soak.sh drill panic      (panic flatten + step-up release)"
    echo "   Track progress: deploy/soak.sh status"
    ;;
  status)
    if [ ! -f "${STATE_DIR}/started_at" ]; then
      echo "soak not started — run: deploy/soak.sh start"; exit 1
    fi
    START=$(cat "${STATE_DIR}/started_at")
    ELAPSED_D=$(( ( $(date -u +%s) - $(date -u -d "${START}" +%s) ) / 86400 ))
    echo "== soak status =="
    echo "   started:  ${START}  (day ${ELAPSED_D} of 14)"
    echo "   release:  $(cat "${STATE_DIR}/release_commit" | cut -c1-12)"
    echo "-- release-readiness"
    readiness || echo "!! readiness endpoint unreachable"
    echo ""
    echo "-- drills logged so far"
    grep -c "DRILL" "${LOG_FILE}" 2>/dev/null | xargs -I{} echo "   {} drill entries in ${LOG_FILE}"
    echo "-- exit criteria (verify before sign-off)"
    echo "   [ ] zero unresolved-accepted states older than 5 min (readiness: reconciliation.ok)"
    echo "   [ ] zero duplicate orders across the full window"
    echo "   [ ] reconciliation delay p95 < 60 s"
    echo "   [ ] all alerts delivered during drills"
    echo "   [ ] audit log complete for every sensitive action"
    ;;
  drill)
    case "${2:?usage: deploy/soak.sh drill [alert|recovery|panic]}" in
      alert)
        echo "== ALERT DRILL: killing worker-trading =="
        docker compose kill worker-trading
        echo "-- waiting 90s (lease TTL 45s — the lease must expire and alerting must fire)"
        sleep 90
        echo "-- readiness during outage (workers.trading.alive should be false):"
        readiness || true
        echo ""
        echo "-- restarting worker-trading"
        docker compose up -d worker-trading
        echo "-- waiting 60s for lease re-acquisition"
        sleep 60
        readiness || true
        log_entry "DRILL alert — worker-trading killed + recovered. VERIFY: alert was delivered (Telegram/email) and lease re-acquired."
        ;;
      recovery)
        echo "== RECOVERY DRILL: backup + dry-run restore validation =="
        deploy/backup.sh backup
        LATEST=$(ls -t backups/stoic-mongo-*.archive.gz | head -1)
        echo "-- validating ${LATEST} (dry run, no data touched)"
        docker compose exec -T mongo sh -c 'mongorestore -u "$MONGO_INITDB_ROOT_USERNAME" -p "${MONGO_INITDB_ROOT_PASSWORD:-$(cat "$MONGO_INITDB_ROOT_PASSWORD_FILE")}" --authenticationDatabase admin --archive --gzip --dryRun' < "${LATEST}"
        log_entry "DRILL recovery — ${LATEST} validated via dry-run restore. VERIFY: for the full drill, restore into a staging copy and compare equity/trade counts."
        ;;
      panic)
        echo "== PANIC DRILL (manual — requires step-up MFA) =="
        echo "   1. In the UI: trigger PANIC (flatten all positions)."
        echo "   2. Verify every open position closes at the broker and the audit log records it."
        echo "   3. Release panic — confirm the step-up MFA modal gates the release."
        echo "   4. Confirm the bot resumes only after explicit re-enable."
        echo "   Then record it:  deploy/soak.sh log \"DRILL panic — flatten + step-up release verified\""
        ;;
      *) echo "usage: deploy/soak.sh drill [alert|recovery|panic]"; exit 1 ;;
    esac
    ;;
  log)
    log_entry "${2:?usage: deploy/soak.sh log \"<note>\"}"
    ;;
  *)
    echo "usage: deploy/soak.sh [start|status|drill alert|recovery|panic|log \"<note>\"]"; exit 1 ;;
esac
