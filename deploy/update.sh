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
. deploy/preflight.sh
capture_readiness_policy || exit 1            # audit H1: policy is process-local, scrubbed from env

REF="origin/main"
for a in "$@"; do
  case "$a" in
    --onboarding-close-only) STOIC_DEPLOY_POLICY=onboarding-close-only ;;
    --no-host-changes) PREFLIGHT_NO_HOST_CHANGES=1 ;;   # refuse instead of applying missing host prerequisites
    --yes|-y) PREFLIGHT_YES=1 ;;                        # no prompts (release key pin, dockerd restart)
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
  ensure_backup_passphrase || exit 1    # N101-6 — never reach backup.sh without a passphrase for secrets/
  deploy/backup.sh backup
  # R-1 — remember THIS archive: an auto-rollback restores code AND data together
  # (main94 rewrites bridge tokens + indexes at first boot; old code cannot start on new data).
  PRE_BACKUP="$(ls -t "${BACKUP_DIR:-./backups}"/stoic-mongo-*.archive.gz "${BACKUP_DIR:-./backups}"/stoic-mongo-*.archive.gz.enc 2>/dev/null | head -1 || true)"
  [ -n "${PRE_BACKUP}" ] && echo "   rollback archive: ${PRE_BACKUP}"

  echo "-- fetching ${REF}"
  git fetch --all --tags --prune
  # N100-1 — main99's installer wrote deploy/env/* over the (then tracked) template files; main100
  # removes them from git, so a modified copy makes `git checkout` refuse. Templates carry no
  # secrets (backend/.env is untouched) → restore them before switching trees.
  # N101-1 — the adopted authoritative rc_lock/BUILD_SHA are tracked too (kept per commit in deploy/releases/).
  restore_tracked_release_files
  # M117-4 — operators hot-patch deploy/*.sh on the host (e.g. during a deploy jam); modified or staged tracked
  # files make `git checkout` refuse AFTER the backup. The deployed tree must equal the signed release, so the
  # edits (staged + unstaged, incl. hand-added files) are saved as a patch (deploy/releases/local-changes-<ts>.patch)
  # and the tracked tree is reset to HEAD — never silently lost; untracked files (.env, secrets/, backups/) untouched.
  DIRTY=$(git status --porcelain --untracked-files=no)
  if [ -n "${DIRTY}" ]; then
    mkdir -p deploy/releases
    PATCH="deploy/releases/local-changes-$(date -u +%Y%m%dT%H%M%SZ).patch"
    git diff HEAD > "${PATCH}"
    echo "!! local modifications to tracked files saved to ${PATCH} and discarded (the deployed tree must equal the signed release):"
    echo "${DIRTY}" | sed 's/^/     /'
    git reset -q --hard HEAD
  fi
  git checkout --detach "${REF}"
  if [ "$(git rev-parse HEAD)" = "${PREV}" ]; then
    echo "   already on $(git rev-parse --short HEAD) — nothing to publish"
    exit 0
  fi
  # The scripts sourced above came from the OLD checkout. Re-exec so the rest of
  # the update (gates, build, verification, rollback policy) runs with the NEW
  # deploy/update.sh + deploy/lib.sh.
  # The policy is handed over EXPLICITLY (H1) — the re-exec'd script captures and scrubs it again.
  exec env STOIC_UPDATE_REEXEC=1 STOIC_UPDATE_PREV="${PREV}" STOIC_UPDATE_BACKUP="${PRE_BACKUP}" PREFLIGHT_NO_HOST_CHANGES="${PREFLIGHT_NO_HOST_CHANGES}" PREFLIGHT_YES="${PREFLIGHT_YES}" STOIC_READINESS_POLICY="$(readiness_policy)" bash deploy/update.sh "${REF}"
fi

# Pre-build gates fail BEFORE anything on the host changed: restore the checkout
# and stop — never rebuild/restart the running stack for a refused release.
gate_refused() {
  echo "!! release refused before build — nothing was changed; checkout restored to ${PREV}"
  restore_tracked_release_files
  git checkout --detach "${PREV}"
  restore_adopted_lock "${PREV}"
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
    if ! RESTORE_NO_START=1 deploy/backup.sh restore "${PRE_BACKUP}"; then
      # N97-5 / N98-7 — never start old code on a half-restored database AND never leave the
      # new release running (restore may fail at decryption, before it stopped anything).
      docker compose stop >/dev/null 2>&1 || true
      echo "!! database restore FAILED — stack left STOPPED. Restore manually: deploy/rollback.sh ${PREV} --with-db ${PRE_BACKUP}"
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD) auto-rollback-db-restore-FAILED=${PRE_BACKUP} stack-stopped" >> deploy/releases.log
      exit 1
    fi
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD) auto-rollback-db-restore=${PRE_BACKUP}" >> deploy/releases.log
  else
    echo "!! no pre-update archive to restore (or UPDATE_ROLLBACK_RESTORE_DB=0) — database keeps the NEW release's state"
  fi
  restore_tracked_release_files
  git checkout --detach "${PREV}"
  restore_adopted_lock "${PREV}"
  if [ "$(deploy_mode)" = "registry" ]; then verify_attestation >/dev/null 2>&1 || true; fi
  provision_images || true
  compose_up
  echo "!! rolled back to $(git rev-parse --short HEAD). Inspect: docker compose logs backend --tail 100"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD) auto-rollback-from=${REF}" >> deploy/releases.log
  exit 1
}

echo "-- release attestation gate (signed CI record: SHA · tests · scans · gates)"
verify_attestation || gate_refused

echo "-- release lock adoption (N101-1: authoritative rc_lock + BUILD_SHA from the signed release assets)"
adopt_release_lock || gate_refused

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

# A14-1 — env templates: deploy/env/ is the source; materialise the dot-files, then refuse drift
python3 scripts/sync_env_examples.py >/dev/null || { echo "!! env templates could not be materialised from deploy/env/"; gate_refused; }
# cPanel/RHEL-8 preflight — BEFORE any build/pull/recreate: host prerequisites (fs.may_detach_mounts,
# docker root slave), leftovers of a previous jam, CI release key pin, shared-host profile
preflight_host || gate_refused
clean_leftovers || gate_refused
ensure_release_public_key_pin
record_host_profile
echo "-- provisioning images ($(deploy_mode): build with provenance | pull attested GHCR digests)"
ensure_release_secrets || gate_refused   # N100-7 — nothing is built yet: refuse, never restore the database
ensure_installation_id || gate_refused   # N104-3/N105-4 — installation id: secrets/installation_id ↔ backend/.env (mismatch refuses)
ensure_bundle_key_pins                   # N101-5 — runtime key id/pin; CI release token never on the API host
ensure_backup_passphrase || gate_refused # N101-6 — second stage too (first stage may have run an older script)
# N-R1 — hosts installed before the trusted-proxy chain existed: default the docker ranges once
grep -q "^TRUSTED_PROXY_CIDRS=." backend/.env 2>/dev/null || set_kv backend/.env TRUSTED_PROXY_CIDRS "172.16.0.0/12,10.0.0.0/8,192.168.0.0/16,127.0.0.0/8,::1/128,fd00::/8"
provision_images || rollback

echo "-- restarting stack (one recreate; overlay EBUSY → clean once, retry once, else stop with the reboot recipe)"
set +e; compose_up_guarded; UP_RC=$?; set -e
if [ "${UP_RC}" = 2 ]; then
  # M114-2 — never leave worker-trading running while protection/reconciliation may be down
  pause_trading_after_jam "${REF}"
  echo "!! stack left as-is for the reboot (no auto-rollback: a rollback would hit the same EBUSY). After the reboot: deploy/update.sh ${REF} (clears TRADING PAUSED)"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD) update-jammed-ebusy ref=${REF} reboot-required" >> deploy/releases.log
  exit 1
fi
[ "${UP_RC}" = 0 ] || rollback
clear_deploy_jam_marker

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

# round-5 P1 / round-6 P1 — topology policy gate. RECONCILE_EXPECT in ./.env is either `approved`
# (A20-P0-02: accounts/enabled/bots AND the exact account ids come from the SIGNED policy approved in
# Admin → Inventory — never a fixed 6/3/3) or an explicit N/N/N that must equal RECONCILE_APPROVED_POLICY.
# In PRODUCTION the gate is MANDATORY and fail-closed: the variable must exist (default: approved),
# the reconciliation must be scoped to the production tenant (RECONCILE_SCOPE_USER_ID) and the
# evidence must carry a non-null signature from the dedicated key. Any missing element → rollback.
_envval() { { grep -E "^$1=" .env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d '"'"'"; }   # never non-zero under pipefail
_benvval() { { grep -E "^$1=" backend/.env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d '"'"'"; }   # what the backend process actually loads
APP_ENV_VAL=$(app_env)   # production if ./.env OR backend/.env says so (lib.sh — the backend reads backend/.env)
RECONCILE_EXPECT=$(_envval RECONCILE_EXPECT)
RECONCILE_SCOPE=$(_envval RECONCILE_SCOPE_USER_ID)
APPROVED_POLICY=$(_envval RECONCILE_APPROVED_POLICY); APPROVED_POLICY="${APPROVED_POLICY:-approved}"
if [ "${APP_ENV_VAL}" = "production" ] && [ "${ONBOARDING}" = 1 ]; then
  echo "!! topology policy gate SKIPPED (onboarding-close-only) — RECONCILE_EXPECT=${RECONCILE_EXPECT:-unset}; trading stays CLOSE_ONLY until a full deploy/update.sh passes"
  RECONCILE_EXPECT=""
fi
if [ "${APP_ENV_VAL}" = "production" ] && [ "${ONBOARDING}" = 0 ]; then
  [ -n "${RECONCILE_EXPECT}" ] || { echo "!! production requires RECONCILE_EXPECT in .env — RECONCILE_EXPECT=approved takes the counts from the signed policy (approved policy: ${APPROVED_POLICY})"; rollback; }
  echo "${RECONCILE_EXPECT}" | grep -Eq '^([0-9]{1,4}/[0-9]{1,4}/[0-9]{1,4}|approved)$' || { echo "!! RECONCILE_EXPECT='${RECONCILE_EXPECT}' malformed (want N/N/N) or 'approved'"; rollback; }
  if [ "${RECONCILE_EXPECT}" != approved ]; then
    [ "${RECONCILE_EXPECT}" = "${APPROVED_POLICY}" ] || { echo "!! RECONCILE_EXPECT=${RECONCILE_EXPECT} differs from the approved policy ${APPROVED_POLICY} (set RECONCILE_EXPECT=approved to use the signed policy itself)"; rollback; }
  fi
  [ -n "${RECONCILE_SCOPE}" ] || { echo "!! production requires RECONCILE_SCOPE_USER_ID (explicit tenant scope)"; rollback; }
  [ -n "$(_envval LEDGER_ANCHOR_KEY)" ] || [ -s secrets/ledger_anchor_key ] || { echo "!! production requires LEDGER_ANCHOR_KEY (dedicated evidence signing key — secrets/ledger_anchor_key or ./.env)"; rollback; }
  # N99-5 — the backend reads backend/.env (never ./.env); check the file the process sees
  [ -n "$(_benvval BRIDGE_TOKEN_HASH_KEY)" ] || [ -s secrets/bridge_token_hash_key ] || { echo "!! production requires BRIDGE_TOKEN_HASH_KEY (dedicated EA token hash key — secrets/bridge_token_hash_key; generated once by ensure_release_secrets)"; rollback; }
  if [ -n "$(_benvval BRIDGE_TOKEN_HASH_KEY)" ] && [ -s secrets/bridge_token_hash_key ] && [ "$(_benvval BRIDGE_TOKEN_HASH_KEY)" != "$(cat secrets/bridge_token_hash_key)" ]; then
    echo "!! BRIDGE_TOKEN_HASH_KEY in backend/.env differs from secrets/bridge_token_hash_key — unify them first (see docs/DISASTER_RECOVERY.md)"; rollback
  fi
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
