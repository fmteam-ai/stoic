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
#   STOIC_READINESS_POLICY=onboarding-close-only deploy/update.sh [ref]
#     (or: deploy/update.sh [ref] --onboarding-close-only) → on a PRODUCTION host,
#     publish while the operator release gates (inventory approval, EA release
#     record, canonical decision, topology policy) are still pending. Needed when the
#     running build predates the Admin tooling that clears those gates. The backend
#     keeps trading fail-closed (CLOSE_ONLY) until they clear — this flag never opens
#     trading, it only lets the code that clears the gates reach the host.
set -euo pipefail
cd "$(dirname "$0")/.."
. deploy/lib.sh
capture_readiness_policy || exit 1            # audit H1: policy is process-local, scrubbed from env

REF="origin/main"
for a in "$@"; do
  case "$a" in
    --onboarding-close-only) STOIC_DEPLOY_POLICY=onboarding-close-only ;;
    *) REF="$a" ;;
  esac
done
ONBOARDING=0; [ "$(readiness_policy)" = "onboarding-close-only" ] && ONBOARDING=1
LOCK=/tmp/stoic-deploy.lock

if [ -n "${STOIC_UPDATE_REEXEC:-}" ]; then
  # second stage: already fetched + checked out by the first stage; the flock
  # on fd 9 was inherited across exec. Resume with the NEW scripts.
  PREV="${STOIC_UPDATE_PREV}"
  PRE_BACKUP="${STOIC_UPDATE_BACKUP:-}"
  echo "   deploy scripts refreshed → continuing with $(git rev-parse --short HEAD)'s deploy/update.sh"
else
  PREV=$(git rev-parse HEAD)
  exec 9>"${LOCK}"; flock -n 9 || { echo "ERROR: another deploy is running (${LOCK})"; exit 1; }

  echo "== STOIC update: $(git rev-parse --short HEAD) -> ${REF} =="

  echo "-- pre-update backup"
  deploy/backup.sh backup
  # R-1 — remember THIS archive: an auto-rollback restores code AND data together
  # (main94 rewrites bridge tokens + indexes at first boot; old code cannot start on new data).
  PRE_BACKUP="$(ls -t "${BACKUP_DIR:-./backups}"/stoic-mongo-*.archive.gz "${BACKUP_DIR:-./backups}"/stoic-mongo-*.archive.gz.enc 2>/dev/null | head -1 || true)"
  [ -n "${PRE_BACKUP}" ] && echo "   rollback archive: ${PRE_BACKUP}"

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
  # The policy is handed over EXPLICITLY (H1) — the re-exec'd script captures and scrubs it again.
  exec env STOIC_UPDATE_REEXEC=1 STOIC_UPDATE_PREV="${PREV}" STOIC_UPDATE_BACKUP="${PRE_BACKUP}" STOIC_READINESS_POLICY="$(readiness_policy)" bash deploy/update.sh "${REF}"
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
  # Q-3 / R-1 — order: safety dump → stop + restore pre-update DATA (no restart) → old CODE → start.
  # Opt out with UPDATE_ROLLBACK_RESTORE_DB=0 (data written by the new release is then kept).
  if [ "${UPDATE_ROLLBACK_RESTORE_DB:-1}" = "1" ] && [ -n "${PRE_BACKUP:-}" ] && [ -f "${PRE_BACKUP}" ]; then
    echo "-- safety dump of the FAILED release's database state"
    deploy/backup.sh backup || true
    echo "-- restoring pre-update database ${PRE_BACKUP} (stack stopped; old code starts on it next)"
    RESTORE_NO_START=1 deploy/backup.sh restore "${PRE_BACKUP}" || echo "!! database restore FAILED — restore manually: deploy/rollback.sh ${PREV} --with-db ${PRE_BACKUP}"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD) auto-rollback-db-restore=${PRE_BACKUP}" >> deploy/releases.log
  else
    echo "!! no pre-update archive to restore (or UPDATE_ROLLBACK_RESTORE_DB=0) — database keeps the NEW release's state"
  fi
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
  if [ "${ONBOARDING}" = 1 ]; then
    echo "!! STOIC_READINESS_POLICY=onboarding-close-only on a PRODUCTION host — operator release gates (ea_release · inventory · canonical_decision · topology policy) are REPORTED, not enforced, for this publish"
    echo "   trading stays fail-closed (CLOSE_ONLY) in the backend until every gate is green; clear them in Admin → Inventory & Go-Live Gate, then re-run deploy/update.sh WITHOUT the flag"
  else
    echo "-- production pre-build gate (operator state a rebuild cannot change: EA release record · inventory · canonical decision)"
    strict_prebuild_gate || gate_refused
  fi
fi

echo "-- provisioning images ($(deploy_mode): build with provenance | pull attested GHCR digests)"
ensure_release_secrets
# N-R1 — hosts installed before the trusted-proxy chain existed: default the docker ranges once
grep -q "^TRUSTED_PROXY_CIDRS=." backend/.env 2>/dev/null || set_kv backend/.env TRUSTED_PROXY_CIDRS "172.16.0.0/12,10.0.0.0/8,192.168.0.0/16,127.0.0.0/8,::1/128,fd00::/8"
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
if [ "${APP_ENV_VAL}" = "production" ] && [ "${ONBOARDING}" = 1 ]; then
  echo "!! topology policy gate SKIPPED (onboarding-close-only) — RECONCILE_EXPECT=${RECONCILE_EXPECT:-unset}; trading stays CLOSE_ONLY until a full deploy/update.sh passes"
  RECONCILE_EXPECT=""
fi
if [ "${APP_ENV_VAL}" = "production" ] && [ "${ONBOARDING}" = 0 ]; then
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

mkdir -p deploy/releases
printf '%s' "${BODY}" | ONBOARDING="${ONBOARDING}" python3 -c '
import json, os, sys
d = json.load(sys.stdin); c = d.get("checks") or {}
pending = sorted(k for k, v in c.items() if isinstance(v, dict) and v.get("ok") is False)
onb = os.environ["ONBOARDING"] == "1"
state = "release_ready" if d.get("ready") else ("onboarding_close_only" if onb else "release_gates_pending")
posture = "OPEN" if d.get("ready") else "CLOSE_ONLY"
json.dump({"deployment_state": state, "readiness_policy": "onboarding-close-only" if onb else "release-ready",
           "release_ready": bool(d.get("ready")), "pending_gates": pending, "trading_posture": posture},
          open("deploy/releases/deployment_state.json", "w"), sort_keys=True, indent=1)
tail = (" · pending gates: " + ", ".join(pending)) if pending else ""
print("   deployment state: " + state + " · trading posture: " + posture + tail)
' || echo "!! could not record deploy/releases/deployment_state.json"

echo "   API + frontend + full topology verified on $(git rev-parse --short HEAD)"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD) update-from=$(git rev-parse --short "${PREV}")$([ "${ONBOARDING}" = 1 ] && echo ' policy=onboarding-close-only')" >> deploy/releases.log
docker compose ps --format '{{.Name}}\t{{.Status}}'
echo "== update complete =="
