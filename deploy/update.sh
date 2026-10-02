#!/usr/bin/env bash
# STOIC update (re-publish): backup → pull target ref → rebuild WITH build
# provenance → restart → verify the COMPLETE trading topology (API, running
# build SHA, 6 workers, Mongo round trip, reconciliation lag, outbox backlog,
# schema compatibility, frontend), with automatic rollback to the previous
# ref if any verification fails.
#   deploy/update.sh             → latest origin/main
#   deploy/update.sh v1.4.2      → a tag
#   deploy/update.sh <sha>       → an exact commit
#   UPDATE_HOLD_ON_FAILURE=1 deploy/update.sh → keep the new build running on
#   verification failure (print failing checks, no auto-rollback) for inspection
set -euo pipefail
cd "$(dirname "$0")/.."
. deploy/lib.sh

REF="${1:-origin/main}"
LOCK=/tmp/stoic-deploy.lock

if [ -n "${STOIC_UPDATE_REEXEC:-}" ]; then
  # second stage: already fetched + checked out by the first stage; the flock
  # on fd 9 was inherited across exec. Resume with the NEW scripts.
  PREV="${STOIC_UPDATE_PREV}"
  echo "   deploy scripts refreshed → continuing with $(git rev-parse --short HEAD)'s deploy/update.sh"
else
  PREV=$(git rev-parse HEAD)
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
  # The scripts sourced above came from the OLD checkout. Re-exec so the rest of
  # the update (gates, build, verification, rollback policy) runs with the NEW
  # deploy/update.sh + deploy/lib.sh.
  exec env STOIC_UPDATE_REEXEC=1 STOIC_UPDATE_PREV="${PREV}" bash deploy/update.sh "${REF}"
fi

# Pre-build gates fail BEFORE anything on the host changed: restore the checkout
# and stop — never rebuild/restart the running stack for a refused release.
gate_refused() {
  echo "!! release refused before build — nothing was changed; checkout restored to ${PREV}"
  git checkout --detach "${PREV}"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD) gate-refused ref=${REF}" >> deploy/releases.log
  exit 1
}

rollback() {
  if [ "${UPDATE_HOLD_ON_FAILURE:-0}" = "1" ]; then
    echo "!! verification failed — UPDATE_HOLD_ON_FAILURE=1: leaving $(git rev-parse --short HEAD) running for inspection"
    echo "   inspect: docker compose logs backend --tail 100 · deploy/doctor.sh · revert with deploy/rollback.sh"
    exit 1
  fi
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
verify_attestation || gate_refused

echo "-- release provenance gate (BUILD_SHA · rc_lock · model manifest · test manifest bind to one commit)"
verify_release_provenance || gate_refused

if [ "$(app_env)" = "production" ]; then
  echo "-- production pre-build gate (operator state a rebuild cannot change: EA release record · inventory · canonical decision)"
  strict_prebuild_gate || gate_refused
fi

echo "-- provisioning images ($(deploy_mode): build with provenance | pull attested GHCR digests)"
ensure_release_secrets
provision_images || rollback

echo "-- restarting stack"
compose_up || rollback

echo "-- verifying API health"
wait_api_health 30 || rollback
echo "   API healthy"
verify_running_sha || rollback

echo "-- verifying frontend"
wait_frontend 30 || rollback
echo "   frontend serving"

echo "-- verifying release readiness (workers, leases, Mongo, reconciliation, outbox, schema)"
BODY=$(wait_release_ready 45) || rollback
echo "   release-readiness: ${BODY}"

if grep -q 'docker-compose.forecast.yml' .env 2>/dev/null; then
  echo "-- verifying forecast profile"
  docker compose exec -T worker-trading python ops/verify_forecast_profile.py || rollback
fi

# round-5 P1 / round-6 P1 — topology policy gate. RECONCILE_EXPECT=accounts/enabled/bots
# in ./.env (e.g. 6/3/3). In PRODUCTION the gate is MANDATORY and fail-closed:
# the variable must exist, match N/N/N, equal the approved policy
# (RECONCILE_APPROVED_POLICY, default 6/3/3), the reconciliation must be scoped
# to the production tenant (RECONCILE_SCOPE_USER_ID) and the evidence must carry
# a non-null signature from the dedicated key. Any missing element → rollback.
_envval() { { grep -E "^$1=" .env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d '"'"'"; }   # never non-zero under pipefail
APP_ENV_VAL=$(app_env)   # production if ./.env OR backend/.env says so (lib.sh — the backend reads backend/.env)
RECONCILE_EXPECT=$(_envval RECONCILE_EXPECT)
RECONCILE_SCOPE=$(_envval RECONCILE_SCOPE_USER_ID)
APPROVED_POLICY=$(_envval RECONCILE_APPROVED_POLICY); APPROVED_POLICY="${APPROVED_POLICY:-6/3/3}"
if [ "${APP_ENV_VAL}" = "production" ]; then
  [ -n "${RECONCILE_EXPECT}" ] || { echo "!! production requires RECONCILE_EXPECT in .env (approved policy ${APPROVED_POLICY})"; rollback; }
  echo "${RECONCILE_EXPECT}" | grep -Eq '^[0-9]{1,4}/[0-9]{1,4}/[0-9]{1,4}$' || { echo "!! RECONCILE_EXPECT='${RECONCILE_EXPECT}' malformed (want N/N/N)"; rollback; }
  [ "${RECONCILE_EXPECT}" = "${APPROVED_POLICY}" ] || { echo "!! RECONCILE_EXPECT=${RECONCILE_EXPECT} differs from the approved policy ${APPROVED_POLICY}"; rollback; }
  [ -n "${RECONCILE_SCOPE}" ] || { echo "!! production requires RECONCILE_SCOPE_USER_ID (explicit tenant scope)"; rollback; }
  [ -n "$(_envval LEDGER_ANCHOR_KEY)" ] || [ -s secrets/ledger_anchor_key ] || { echo "!! production requires LEDGER_ANCHOR_KEY (dedicated evidence signing key — secrets/ledger_anchor_key or ./.env)"; rollback; }
fi
if [ -n "${RECONCILE_EXPECT}" ]; then
  echo "-- verifying production topology policy (${RECONCILE_EXPECT}, signed read-only reconciliation)"
  mkdir -p release/evidence
  EVIDENCE="release/evidence/production-reconcile-$(git rev-parse --short HEAD).json"
  SCOPE_ARGS=(); [ -n "${RECONCILE_SCOPE}" ] && SCOPE_ARGS=(--scope-user "${RECONCILE_SCOPE}")
  STRICT_ARGS=(); [ "${APP_ENV_VAL}" = "production" ] && STRICT_ARGS=(--strict)
  if docker compose exec -T -e GIT_SHA="${GIT_SHA}" -e APP_ENV="${APP_ENV_VAL}" backend \
       python ops/production_reconcile.py --expect "${RECONCILE_EXPECT}" "${SCOPE_ARGS[@]}" "${STRICT_ARGS[@]}" \
       > "${EVIDENCE}" 2>/dev/null; then
    # the evidence itself must be signed with a real key — never accept an unsigned PASS
    python3 - "${EVIDENCE}" <<'PY' || { echo "!! reconciliation evidence unsigned or malformed — refusing this release"; rollback; }
import json, sys
d = json.load(open(sys.argv[1]))
ok = d.get("result") == "PASS" and isinstance(d.get("signature"), str) and len(d["signature"]) == 64 and d.get("build") not in (None, "", "unknown")
sys.exit(0 if ok else 1)
PY
    echo "   topology policy ${RECONCILE_EXPECT}: PASS, signed (${EVIDENCE})"
    if [ "${APP_ENV_VAL}" = "production" ]; then
      # round-7 P1 — the signed pre-promotion bundle proves the FULL trading truth for THIS build
      PP="release/evidence/prepromotion-$(git rev-parse --short HEAD).json"
      if docker compose exec -T -e GIT_SHA="${GIT_SHA}" -e APP_ENV=production backend \
           python ops/prepromotion_evidence.py --expect "${RECONCILE_EXPECT}" --scope-user "${RECONCILE_SCOPE}" > "${PP}" 2>/dev/null; then
        echo "   pre-promotion evidence: PASS, signed (${PP})"
      else
        echo "!! pre-promotion evidence FAILED (see ${PP}: failed_gates) — refusing this release"; rollback
      fi
    fi
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
