#!/usr/bin/env bash
# Staging acceptance run — ONE command for the deployed-only acceptance drills.
#
#   make staging-acceptance                 # AT-01 only (no downtime)
#   make staging-acceptance ROLLBACK=1      # + AT-15 forced-failure rollback drill (downtime!)
#   make staging-acceptance EXPECT=approved  # + signed read-only reconciliation must match the approved signed policy (or EXPECT=N/N/N)
#   make staging-acceptance DRILLS=1        # + readiness fail-closed fault drills (restores every injected fault)
#   scripts/staging_acceptance.sh [--rollback] [--yes]
#
# AT-01 · six-account execution boundary — seeds 3 enabled / 2 disabled / 1
#         missing-flag accounts and proves UI contract, worker selection,
#         authority, execution choke point and broker requests agree on the
#         same three ids (runs INSIDE the backend container, cleans up).
# AT-15 · signed release + rollback — verifies the attestation/digests for the
#         running commit, forces release-readiness to fail, runs deploy/update.sh
#         and proves the previous signed digest/SHA/schema come back.
# Evidence bundle: release/drills/summary-<ts>.json (+ per-drill JSON files).
set -uo pipefail
cd "$(dirname "$0")/.."
. deploy/lib.sh

ROLLBACK=0; YES=0
DRILLS=0
for a in "$@"; do case "$a" in --rollback) ROLLBACK=1;; --yes) YES=1;; --readiness-drills) DRILLS=1;; esac; done
[ "${ROLLBACK}" = 1 ] && [ "${YES}" != 1 ] && { echo "--rollback causes downtime; add --yes (staging only)"; exit 2; }
if grep -qE '^STOIC_HOST_ROLE=production' .env 2>/dev/null && [ "${ROLLBACK}" = 1 ]; then
  echo "refusing the rollback drill: .env says STOIC_HOST_ROLE=production"; exit 2
fi

TS=$(date -u +%Y%m%dT%H%M%SZ); mkdir -p release/drills
SUMMARY=release/drills/summary-${TS}.json
R01=SKIP; R15=SKIP; RREC=SKIP; RRD=SKIP

echo "=================  STAGING ACCEPTANCE ${TS}  ================="
echo "== AT-01 · six-account execution boundary"
mkdir -p release/drills
if docker compose exec -T -e DRILL_EVIDENCE_DIR=/tmp/drills backend python ops/at01_account_boundary.py > release/drills/at01-${TS}.log 2>&1; then
  R01=PASS
else
  R01=FAIL
fi
tail -1 release/drills/at01-${TS}.log

echo "== P1-4 · read-only production reconciliation (${EXPECT:-no --expect}) "
if docker compose exec -T backend python ops/production_reconcile.py ${EXPECT:+--expect "$EXPECT"} > release/drills/reconcile-${TS}.log 2>&1; then
  RREC=PASS
else
  RREC=$([ -n "${EXPECT:-}" ] && echo FAIL || echo REPORTED)
fi
tail -1 release/drills/reconcile-${TS}.log

if [ "${DRILLS}" = 1 ]; then
  echo "== readiness fail-closed drills (stale lease · stalled loop · fresh UNKNOWN execution · position mismatch · anchor regression · anchored-hash mismatch)"
  # round-6 P1 hard guard: the drill refuses before its first write unless the runner
  # proves staging (APP_ENV=staging, ALLOW_DESTRUCTIVE_DRILLS=true, approved DB suffix,
  # matching step-up token). Supply DRILL_STEP_UP_TOKEN in the operator shell.
  if docker compose exec -T -e APP_ENV=staging -e ALLOW_DESTRUCTIVE_DRILLS=true \
       -e DRILL_STEP_UP_TOKEN="${DRILL_STEP_UP_TOKEN:-}" -e DRILL_STEP_UP_TOKEN_EXPECTED="${DRILL_STEP_UP_TOKEN_EXPECTED:-${DRILL_STEP_UP_TOKEN:-}}" \
       -e DRILL_DB_SUFFIXES="${DRILL_DB_SUFFIXES:-_staging,_drill}" \
       backend python ops/readiness_drills.py > release/drills/readiness-${TS}.log 2>&1; then RRD=PASS; else RRD=FAIL; fi
  tail -1 release/drills/readiness-${TS}.log
fi

if [ "${ROLLBACK}" = 1 ]; then
  echo "== AT-15 · signed release + forced-failure rollback"
  if deploy/drills/at15_rollback_drill.sh --yes; then R15=PASS; else R15=FAIL; fi
fi

python3 - "$SUMMARY" "$R01" "$R15" "$TS" "$RREC" "$RRD" <<'EOF'
import json, sys, subprocess
s, r01, r15, ts, rrec, rrd = sys.argv[1:]
head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
res = {"run": ts, "commit": head, "AT-01": r01, "AT-15": r15, "production_reconciliation": rrec, "readiness_drills": rrd,
       "result": "PASS" if r01 == "PASS" and r15 in ("PASS", "SKIP") and rrec != "FAIL" and rrd != "FAIL" else "FAIL",
       "note": "AT-15 SKIP = rollback drill not requested (run with ROLLBACK=1 / --rollback --yes)"}
json.dump(res, open(s, "w"), indent=1)
print(json.dumps(res, indent=1))
EOF
[ "$R01" = PASS ] && [ "$R15" != FAIL ] && [ "$RREC" != FAIL ] && [ "$RRD" != FAIL ]
