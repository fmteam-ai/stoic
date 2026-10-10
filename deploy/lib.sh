#!/usr/bin/env bash
# Shared helpers for deploy/install.sh · update.sh · rollback.sh
# Source from the repo root:  . deploy/lib.sh

# Deploy scripts need python >= 3.9 from the PRIVATE dir (audit H2: guarded prepend, see stoic-path.sh).
. "$(dirname "${BASH_SOURCE[0]}")/stoic-path.sh"

# Audit H1 — the readiness policy is a DEPLOY-TIME decision, not an environment fact.
# Capture it once into a non-exported shell variable and scrub it from the environment
# so docker compose, python helpers and child shells can never inherit it.
capture_readiness_policy() {
  STOIC_DEPLOY_POLICY="${1:-${STOIC_READINESS_POLICY:-}}"
  unset STOIC_READINESS_POLICY
  case "${STOIC_DEPLOY_POLICY}" in
    ""|release-ready|onboarding-close-only|infrastructure-only) return 0 ;;
    *) echo "ERROR: unknown readiness policy '${STOIC_DEPLOY_POLICY}' (release-ready | onboarding-close-only | infrastructure-only)" >&2; return 1 ;;
  esac
}
readiness_policy() { printf '%s' "${STOIC_DEPLOY_POLICY:-}"; }

# Set-or-append KEY=VALUE in a dotenv file (idempotent).
set_kv() {
  if grep -q "^$2=" "$1" 2>/dev/null; then
    sed -i.bak "s|^$2=.*|$2=$3|" "$1" && rm -f "$1.bak"
  else
    printf '%s=%s\n' "$2" "$3" >> "$1"
  fi
}

# APP_ENV as the BACKEND sees it. The API process reads backend/.env (compose
# env_file); ./.env is the installer/compose file. Earlier installers wrote
# production only to backend/.env, so deploy gates keyed off ./.env silently ran
# non-strict on production hosts. Fail closed: production if EITHER file says so.
app_env() {
  local a b
  a=$( { grep -E '^APP_ENV=' .env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d "\"'")
  b=$( { grep -E '^APP_ENV=' backend/.env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d "\"'")
  if [ "${a}" = "production" ] || [ "${b}" = "production" ]; then printf 'production'; else printf '%s' "${b:-${a}}"; fi
}

# Docker secrets introduced by later releases — generated when missing so an
# existing install upgrades without manual steps (install.sh + update.sh).
#   order_auth_secret / ledger_anchor_key / bridge_token_hash_key : keys distinct from JWT (boot rule)
#   secrets_master_key                    : dedicated integrations-vault key (audit r28 P2-02);
#                                           legacy JWT-derived records are migrated at boot
ensure_release_secrets() {
  [ -d secrets ] || return 0
  local f
  # N100-11 — existing installs predate the per-purpose signer token: mint the bundle token the
  # API will hold (the release token stays with CI / the signer only). Compose mounts it next.
  if [ -d secrets ] && [ -s secrets/signer_token ] && [ ! -s secrets/signer_token_bundle ]; then
    ( umask 077; openssl rand -hex 32 > secrets/signer_token_bundle ) && echo "   generated secrets/signer_token_bundle (API-side signer token)"
  fi
  # create with mode 0600 from the start (no umask window), regenerate zero-byte leftovers
  # main98 — bridge_token_hash_key: generated ONCE, never regenerated (changing it un-pairs every EA)
  # N99-5 — a key already living in backend/.env (or ./.env) must become the file value, never a
  # second random one: the env wins at load time and a differing file would un-pair every EA the
  # day the env line is removed. Both present and different ⇒ hard stop.
  local envkey envval
  for f in order_auth_secret ledger_anchor_key bridge_token_hash_key; do
    envkey=$(printf '%s' "${f}" | tr '[:lower:]' '[:upper:]')
    envval=$( { grep -E "^${envkey}=." backend/.env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d "\"'")   # N100-7 — backend/.env only
    if [ -s "secrets/${f}" ] && [ -n "${envval}" ] && [ "$(cat "secrets/${f}")" != "${envval}" ]; then
      echo "!! ${envkey} differs between backend/.env and secrets/${f} — keep ONE value:"
      echo "   copy the .env value into secrets/${f} (or vice-versa), then remove the .env line. Refusing to continue."
      return 1
    fi
    if [ ! -s "secrets/${f}" ]; then
      if [ -n "${envval}" ]; then
        (umask 077; printf '%s\n' "${envval}" > "secrets/${f}"); echo "   seeded ${f} from ${envkey} in .env (remove the .env line once the stack is up)"
      else
        (umask 077; python3 -c "import secrets;print(secrets.token_urlsafe(32))" > "secrets/${f}"); echo "   generated ${f}"
      fi
    fi
  done
  [ -s secrets/secrets_master_key ] || {
    (umask 077; python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())" > secrets/secrets_master_key)
    echo "   generated secrets_master_key (dedicated vault key — sealed integration secrets migrate at boot)"
  }
  # N-R2 — every `file:` secret docker-compose*.yml mounts must exist BEFORE compose up, also
  # on hosts installed before the secret was introduced (update.sh path). Empty placeholder:
  # the operator pastes the BotFather token to enable security alerts.
  [ -f secrets/security_telegram_token ] || { (umask 077; : > secrets/security_telegram_token); echo "   created empty security_telegram_token (paste the BotFather token to enable security alerts)"; }
  chmod 600 secrets/order_auth_secret secrets/ledger_anchor_key secrets/bridge_token_hash_key secrets/secrets_master_key secrets/security_telegram_token
}

# N104-3 — STOIC_INSTALLATION_ID: the host identity every signed policy migration must name
# (inventory_projection.installation_id; production refuses a policy for another id). Nothing ever
# generated it. Minted ONCE into backend/.env — never regenerated (a new id orphans every signed
# policy) — and shown on Demo Readiness + the Inventory go-live panel. No-op until backend/.env exists.
# Called next to ensure_release_secrets in install.sh (after backend/.env is written) and update.sh.
ensure_installation_id() {
  [ -f backend/.env ] || return 0
  local id="" envid
  envid=$( { grep -E '^STOIC_INSTALLATION_ID=.' backend/.env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d "\"'" | tr -d '[:space:]')   # N106-3 — trailing spaces / CRLF never fake a mismatch
  # N105-4 — secrets/installation_id is the durable copy (backup.sh encrypts ./secrets; backend/.env is
  # not backed up): a rebuilt host restores secrets/ and keeps the id every signed policy names.
  if [ -d secrets ] && [ -s secrets/installation_id ]; then
    id=$(tr -d '[:space:]' < secrets/installation_id)
    if [ -n "${envid}" ] && [ "${envid}" != "${id}" ]; then
      echo "!! STOIC_INSTALLATION_ID differs between backend/.env (${envid}) and secrets/installation_id (${id}) — keep ONE value (the signed policies name it). Refusing to continue."
      return 1
    fi
  elif [ -n "${envid}" ]; then
    id="${envid}"
  else
    id="stoic-$(openssl rand -hex 8)"
    echo "   generated STOIC_INSTALLATION_ID=${id} (policy-migration runs must name it — Demo Readiness shows it)"
  fi
  if [ -d secrets ] && [ ! -s secrets/installation_id ]; then
    ( umask 077; printf '%s\n' "${id}" > secrets/installation_id ) && echo "   stored the installation id in secrets/installation_id (included in encrypted backups)"
  fi
  grep -qxF "STOIC_INSTALLATION_ID=${id}" backend/.env || set_kv backend/.env STOIC_INSTALLATION_ID "${id}"   # rewrites a padded/CRLF line clean
}

# N101-6 — backups encrypt secrets/ with BACKUP_PASSPHRASE_FILE; nothing ever provisioned it, so a
# fresh install hard-failed its first backup (and update.sh/rollback.sh with it). Generate ONE
# passphrase OUTSIDE ./secrets and ./backups (it decrypts them), record it in ./.env.
ensure_backup_passphrase() {
  [ -d secrets ] || return 0
  local f
  f="${BACKUP_PASSPHRASE_FILE:-$( { grep -E '^BACKUP_PASSPHRASE_FILE=' .env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d "\"'")}"
  [ -n "${f}" ] || f="${HOME:-/root}/.stoic-backup-pass"
  case "$(readlink -f "${f}" 2>/dev/null || echo "${f}")" in
    "$(readlink -f ./secrets)"/*|"$(readlink -f "${BACKUP_DIR:-./backups}" 2>/dev/null || echo "$PWD/backups")"/*)
      echo "!! BACKUP_PASSPHRASE_FILE=${f} lives inside ./secrets or the backup directory — refusing (it decrypts them)"; return 1 ;;
  esac
  if [ ! -s "${f}" ]; then
    ( umask 077; openssl rand -base64 32 > "${f}" ) || { echo "!! could not write backup passphrase ${f}"; return 1; }
    echo "   generated backup passphrase ${f} — COPY IT TO YOUR PASSWORD MANAGER NOW: without it no backup of secrets/ can be restored"
  fi
  touch .env
  set_kv .env BACKUP_PASSPHRASE_FILE "${f}"
  export BACKUP_PASSPHRASE_FILE="${f}"
}

# N101-5 — hosts installed before the key split: the sidecar key becomes the RUNTIME (bundle) key with its
# own id; RELEASE_PUBLIC_KEY_B64 keeps pinning the CI release key. The CI release token never lives on the
# API host — strip it from backend/.env (compose mounts only secrets/signer_token_bundle).
ensure_bundle_key_pins() {
  [ -s secrets/signer_public_key ] || return 0
  local sidecar; sidecar=$(cat secrets/signer_public_key)
  grep -q "^BUNDLE_PUBLIC_KEY_B64=." backend/.env 2>/dev/null || {
    set_kv backend/.env BUNDLE_PUBLIC_KEY_B64 "${sidecar}"
    set_kv backend/.env BUNDLE_SIGNER_KEY_ID stoic-bundle-ed25519-v1
    set_kv .env SIGNER_KEY_ID stoic-bundle-ed25519-v1
    echo "   pinned the sidecar as runtime key stoic-bundle-ed25519-v1 (BUNDLE_PUBLIC_KEY_B64)"
  }
  # N102-5 / M119-1 — the sidecar key must never double as the CI release pin (root here could sign EA records).
  # Never silently delete the release pin: REFUSE with the exact remedy (rotate the RUNTIME key, keep the pin).
  if [ "$( { grep -E '^RELEASE_PUBLIC_KEY_B64=' backend/.env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d "\"'")" = "${sidecar}" ]; then
    echo "!! SECURITY: RELEASE_PUBLIC_KEY_B64 equals the LOCAL sidecar key — this host holds the CI release PRIVATE key (secrets/signer_ed25519_key)."
    echo "   Refusing to continue (the pin is NOT deleted). Fix, then re-run:"
    echo "     sudo bash deploy/rotate-runtime-key.sh      # new sidecar key, re-pins BUNDLE_*; RELEASE_PUBLIC_KEY_B64 stays the CI release key"
    echo "     sudo bash deploy/update.sh ${REF:-<ref>}"
    return 1
  fi
  if grep -qE '^RELEASE_SIGNER_TOKEN=.' backend/.env 2>/dev/null; then
    sed -i '/^RELEASE_SIGNER_TOKEN=/d' backend/.env
    echo "   removed RELEASE_SIGNER_TOKEN from backend/.env (CI release token — the API holds only the bundle token)"
  fi
}

# N101-1 — a git checkout of a tag carries the DEVELOPER rc_lock (authoritative:false) and an
# unsubstituted BUILD_SHA; the authoritative lock is frozen inside release.yml and shipped as a release
# asset. Adopt it: fetch rc_lock.json + SHA256SUMS(.sig/.pem), verify the checksum file's Sigstore
# signature against the release workflow identity, check the lock's digest + commit, then write it into
# the checkout (release/rc_lock.json, backend/BUILD_SHA) and keep a copy per commit for rollbacks.
adopt_release_lock() {
  # A18 Part 3 — adopt whenever THIS commit's attestation was verified (required or found by auto mode)
  if ! attestation_for_head; then
    attestation_required && { echo "!! release lock: no verified attestation for ${GIT_SHA:-HEAD} — run verify_attestation first"; return 1; }
    echo "   release lock: attestation not available/required — developer lock kept"; publish_release_truth; return 0
  fi
  local dest=release/attestation tag repo ident want got
  repo=$(grep -E '^GITHUB_REPO=' .env 2>/dev/null | cut -d= -f2-); [ -n "${repo}" ] || repo=$(_repo_slug)
  tag=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("tag",""))' release/attestation.current.json 2>/dev/null || true)
  [ -n "${tag}" ] || tag=$(git tag --points-at "${GIT_SHA}" | grep -E '^v[0-9]' | head -1 || true)
  [ -n "${tag}" ] || { echo "!! release lock: no release tag for ${GIT_SHA}"; return 1; }
  for f in rc_lock.json SHA256SUMS SHA256SUMS.sig SHA256SUMS.pem; do
    [ -s "${dest}/${f}" ] || { echo "!! release lock: asset ${f} missing (release_attestation.py fetch)"; return 1; }
  done
  ident="https://github.com/${repo}/.github/workflows/release.yml@refs/tags/${tag}"
  cosign verify-blob "${dest}/SHA256SUMS" --signature "${dest}/SHA256SUMS.sig" --certificate "${dest}/SHA256SUMS.pem" \
      --certificate-identity "${ident}" --certificate-github-workflow-repository "${repo}" \
      --certificate-oidc-issuer https://token.actions.githubusercontent.com >/dev/null 2>&1 \
    || { echo "!! release lock: SHA256SUMS signature invalid or not issued by ${ident}"; return 1; }
  want=$(awk '$2 == "rc_lock.json" {print $1}' "${dest}/SHA256SUMS" | head -1)
  got=$(sha256sum "${dest}/rc_lock.json" | cut -d' ' -f1)
  [ -n "${want}" ] && [ "${want}" = "${got}" ] || { echo "!! release lock: rc_lock.json digest ${got} != signed SHA256SUMS ${want:-absent}"; return 1; }
  python3 - "${dest}/rc_lock.json" "${GIT_SHA}" <<'PY' || { echo "!! release lock: rc_lock.json is not the authoritative lock of ${GIT_SHA}"; return 1; }
import json, sys
lock = json.load(open(sys.argv[1])); sha = sys.argv[2]
ok = (lock.get("authoritative") is True and lock.get("git_commit") == sha and lock.get("source_sha") == sha
      and bool((lock.get("images") or {}).get("backend")) and bool((lock.get("images") or {}).get("frontend")))
sys.exit(0 if ok else 1)
PY
  # N102-3 — the release build also re-signs MODEL_MANIFEST.json and regenerates RELEASE_SUMMARY.md; the
  # checkout's copies never match the lock, so the strict check fails. Validate EVERY asset against the
  # same signed SHA256SUMS first, then write all of them (audit #5 P3: no partially adopted release).
  local f asset adopt=()
  for f in MODEL_MANIFEST.json RELEASE_SUMMARY.md; do
    want=$(awk -v n="${f}" '$2 == n {print $1}' "${dest}/SHA256SUMS" | head -1)
    if [ -z "${want}" ]; then
      [ "${f}" = "MODEL_MANIFEST.json" ] && [ ! -f backend/models_store/MODEL_MANIFEST.json ] && continue
      echo "!! release lock: ${f} is not covered by the signed SHA256SUMS of ${tag} — strict provenance cannot pass"; return 1
    fi
    [ -s "${dest}/${f}" ] || { echo "!! release lock: asset ${f} missing"; return 1; }
    got=$(sha256sum "${dest}/${f}" | cut -d' ' -f1)
    [ "${want}" = "${got}" ] || { echo "!! release lock: ${f} digest ${got} != signed SHA256SUMS ${want}"; return 1; }
    adopt+=("${f}")
  done
  mkdir -p deploy/releases release
  cp "${dest}/rc_lock.json" "deploy/releases/rc_lock-${GIT_SHA}.json"
  cp "${dest}/rc_lock.json" release/rc_lock.json
  printf '%s\n' "${GIT_SHA}" > backend/BUILD_SHA
  for f in "${adopt[@]}"; do
    case "${f}" in MODEL_MANIFEST.json) asset=backend/models_store/MODEL_MANIFEST.json ;; *) asset=docs/RELEASE_SUMMARY.md ;; esac
    cp "${dest}/${f}" "deploy/releases/${f%.*}-${GIT_SHA}.${f##*.}"
    cp "${dest}/${f}" "${asset}"
  done
  echo "   release lock: authoritative rc_lock of ${tag} adopted (signed SHA256SUMS ✓, commit ✓) · BUILD_SHA stamped · model manifest + release summary adopted"
  publish_release_truth
  [ "$(deploy_mode)" != "build" ] || echo "   note: LIVE release gate compares the running image digest with the locked CI digest — a locally BUILT image never matches; remove DEPLOY_MODE=build (auto) for live authority (demo accounts are exempt, N101-2)"
}

# Put a previously adopted authoritative lock back after `git checkout` (rollback to PREV).
restore_adopted_lock() {
  local sha="$1"
  [ -s "deploy/releases/rc_lock-${sha}.json" ] || { publish_release_truth; return 0; }
  cp "deploy/releases/rc_lock-${sha}.json" release/rc_lock.json
  printf '%s\n' "${sha}" > backend/BUILD_SHA
  [ -s "deploy/releases/MODEL_MANIFEST-${sha}.json" ] && cp "deploy/releases/MODEL_MANIFEST-${sha}.json" backend/models_store/MODEL_MANIFEST.json
  [ -s "deploy/releases/RELEASE_SUMMARY-${sha}.md" ] && cp "deploy/releases/RELEASE_SUMMARY-${sha}.md" docs/RELEASE_SUMMARY.md
  publish_release_truth
  echo "   release lock: re-adopted authoritative rc_lock for ${sha}"
}

# Adopted lock + BUILD_SHA (+ model manifest / release summary) are tracked files — restore them so
# `git checkout` can switch trees.
restore_tracked_release_files() {
  local t
  for t in .env.example backend/.env.example release/rc_lock.json backend/BUILD_SHA \
           backend/models_store/MODEL_MANIFEST.json docs/RELEASE_SUMMARY.md; do
    git ls-files --error-unmatch "$t" >/dev/null 2>&1 && git checkout -- "$t" 2>/dev/null || true
  done
}

# After a failed `compose up`, an app container CREATED BY THIS PASS that exited
# or is unhealthy is a boot failure of the new build — not a mount leak. Print its
# log and stop retrying so the cause is visible instead of buried under 6 identical
# attempts. Containers older than the pass (previous build, mid-recreate) are ignored.
app_boot_failure() {
  local since="$1" c name created state status health found=1
  # v1.60.5 — enumerate with `docker inspect` (the `compose ps --format` Go template printed nothing on the
  # CI runner, so an unhealthy backend died without a single log line being shown)
  for c in $(docker compose ps -aq 2>/dev/null); do
    name=$(docker inspect -f '{{.Name}}' "$c" 2>/dev/null | sed 's#^/##')
    echo "${name}" | grep -qE -- '-(backend|worker-[a-z]+|frontend)-[0-9]+$' || continue
    status=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true)
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "$c" 2>/dev/null || true)
    [ "${status}" = "exited" ] || [ "${status}" = "dead" ] || [ "${health}" = "unhealthy" ] || continue
    created=$(date -u -d "$(docker inspect -f '{{.Created}}' "$c" 2>/dev/null)" +%s 2>/dev/null || echo 0)
    state="${status} exit=$(docker inspect -f '{{.State.ExitCode}}' "$c" 2>/dev/null) health=${health}"
    # v1.60.3 — ALWAYS show why an app container is unhealthy/exited (the CI install-from-archive run died with no
    # log because the container predated this pass); only the verdict keeps the "created in this pass" rule
    echo "!! ${name}: ${state} — last log lines:"
    docker logs --tail 60 "$c" 2>&1 | sed 's/^/   | /'
    docker inspect -f '{{if .State.Health}}{{range .State.Health.Log}}   | healthcheck: exit={{.ExitCode}} {{.Output}}{{end}}{{end}}' "$c" 2>/dev/null | tail -3
    [ "${created}" -ge "${since}" ] || continue
    found=0
  done
  return ${found}
}

# Resolve the exact 40-hex commit being deployed (release archive BUILD_SHA
# or git HEAD) and export GIT_SHA for the compose build. Hard-fails without.
resolve_git_sha() {
  GIT_SHA=$(grep -oE '^[0-9a-f]{40}' backend/BUILD_SHA 2>/dev/null || true)
  [ -n "${GIT_SHA}" ] || GIT_SHA=$(git rev-parse HEAD 2>/dev/null || true)
  if ! echo "${GIT_SHA}" | grep -qE '^[0-9a-f]{40}$'; then
    echo "ERROR: Git SHA provenance missing — not a release archive and not a git checkout. Refusing to build."
    return 1
  fi
  export GIT_SHA
  echo "   build provenance: ${GIT_SHA}"
}

# Build all images with provenance, then pin the exact backend image digest
# into ./.env (production boot + risk snapshots require STOIC_IMAGE_DIGEST).
build_with_provenance() {
  resolve_git_sha || return 1
  disable_registry_compose_file   # a build never runs under the registry overlay (pull_policy: never + pinned images)
  docker compose build || { echo "ERROR: image build failed — see the build log above (nothing was restarted)"; return 1; }
  local img
  img=$(docker compose config --images 2>/dev/null | grep -m1 backend || true)
  STOIC_IMAGE_DIGEST=$(docker inspect --format '{{.Id}}' "${img}" 2>/dev/null || true)
  if [ -z "${STOIC_IMAGE_DIGEST}" ]; then
    echo "ERROR: could not resolve the built backend image digest (${img})"
    return 1
  fi
  touch .env
  set_kv .env STOIC_IMAGE_DIGEST "${STOIC_IMAGE_DIGEST}"
  set_kv .env GIT_SHA "${GIT_SHA}"
  export STOIC_IMAGE_DIGEST
  echo "   image provenance: ${STOIC_IMAGE_DIGEST}"
}

# Metrics token for the ops readiness probe (Docker secret or backend/.env).
metrics_token() {
  local t=""
  if [ -f secrets/metrics_token ]; then t=$(cat secrets/metrics_token 2>/dev/null || true)
  else t=$( { grep -E '^METRICS_TOKEN=' backend/.env 2>/dev/null || true; } | cut -d= -f2- | tr -d '"'); fi
  [ -n "${t}" ] || { echo "ERROR: metrics token missing (secrets/metrics_token or METRICS_TOKEN in backend/.env)" >&2; return 1; }
  printf '%s' "${t}"
}

# Wait until /api/health answers 200 (arg: attempts, 2s apart). Returns 1 on timeout.
wait_api_health() {
  local n="${1:-30}" i
  for i in $(seq 1 "$n"); do
    curl -fsS http://127.0.0.1:8001/api/health >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

# nginx in a freshly recreated frontend container accepts connections a few
# seconds after "Started" — a one-shot probe sees "connection reset" and would
# trigger a needless rollback.
wait_frontend() {
  local n="${1:-30}" i
  for i in $(seq 1 "$n"); do
    curl -fsS -o /dev/null http://127.0.0.1:3000 2>/dev/null && return 0
    sleep 2
  done
  return 1
}

# Wait until the FULL topology readiness probe is green (workers need ~45s
# to acquire leases). Prints the body on success; returns 1 on timeout.
# Readiness policy mirrors deploy/install.sh: the INFRASTRUCTURE checks must be
# green; the release GATES (inventory approval, EA proof, CI attestation,
# rc_lock, canonical decision, turnstile) are operator onboarding steps — they
# are REPORTED and only block when APP_ENV=production (fail-closed there).
READINESS_INFRA="mongo_roundtrip workers loop_progress reconciliation outbox schema repair_ledger_anchor execution_truth"

_readiness_eval() {   # stdin: readiness JSON · $1: "strict"|"infra" → exit 0 when acceptable; prints pending gates to stderr
  python3 -c '
import json, sys
mode, infra = sys.argv[1], sys.argv[2].split()
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(2)
c = d.get("checks") or {}
if mode == "strict":
    sys.exit(0 if d.get("ready") else 1)
missing = [k for k in infra if not (c.get(k) or {}).get("ok")]
pending = [k for k, v in c.items() if k not in infra and isinstance(v, dict) and not v.get("ok")]
if pending:
    print("   pending release gates (operator onboarding, " + ("onboarding-close-only: trading stays CLOSE_ONLY" if mode == "onboarding" else "not blocking outside production") + "): " + ", ".join(pending), file=sys.stderr)
sys.exit(0 if not missing else 1)
' "$1" "${READINESS_INFRA}"
}

# Production pre-build gate: operator state that no rebuild can change (EA release
# record in the checkout, inventory approval + canonical decision on the RUNNING
# stack). Refusing here avoids a build → restart → rollback churn for a known outcome.
strict_prebuild_gate() {
  local tok body k bad=""
  python3 scripts/verify_ea_release.py --check >/dev/null 2>&1 || bad="${bad} ea_release"
  tok=$(metrics_token) || return 1
  body=$(curl -sS -H "X-Metrics-Token: ${tok}" http://127.0.0.1:8001/api/ops/release-readiness 2>/dev/null || true)
  if [ -n "${body}" ]; then
    for k in inventory canonical_decision; do
      printf '%s' "${body}" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if (d.get("checks") or {}).get(sys.argv[1], {}).get("ok") else 1)' "$k" 2>/dev/null || bad="${bad} ${k}"
    done
  fi
  [ -z "${bad}" ] && return 0
  echo "!! production gates already failing on the RUNNING stack:${bad} — a rebuild cannot fix these; complete onboarding first (docs/PUBLISH_RUNBOOK.md)"
  return 1
}

# M118-5 — every worker container must report HEALTHY (leader lease held + fresh); a real unhealthy worker fails the update
wait_workers_healthy() {
  local n="${1:-20}" i c name health bad
  for i in $(seq 1 "$n"); do
    bad=""
    for c in $(docker compose ps -q 2>/dev/null); do
      name=$(docker inspect -f '{{.Name}}' "$c" 2>/dev/null | sed 's#^/##')
      echo "${name}" | grep -qE -- '-worker-[a-z]+-[0-9]+$' || continue
      health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c" 2>/dev/null || echo "?")
      [ "${health}" = "healthy" ] || [ "${health}" = "none" ] || bad="${bad} ${name}=${health}"
    done
    [ -z "${bad}" ] && return 0
    sleep 3
  done
  echo "!! workers not healthy after $((n * 3))s:${bad} — leader lease not held/fresh (docker compose logs --tail 30 <worker>)" >&2
  return 1
}

wait_release_ready() {
  local n="${1:-45}" tok body i mode="infra"
  tok=$(metrics_token) || return 1
  if [ "$(app_env)" = "production" ]; then
    if [ "$(readiness_policy)" = "onboarding-close-only" ]; then mode="onboarding"; else mode="strict"; fi
  fi
  for i in $(seq 1 "$n"); do
    body=$(curl -sS -H "X-Metrics-Token: ${tok}" http://127.0.0.1:8001/api/ops/release-readiness 2>/dev/null || true)
    if [ -n "${body}" ] && printf '%s' "${body}" | _readiness_eval "${mode}" 2>/dev/null; then
      printf '%s' "${body}" | _readiness_eval "${mode}" >/dev/null || true   # surface pending gates once
      echo "${body}"; return 0
    fi
    sleep 4
  done
  # callers capture stdout — diagnostics MUST go to stderr or they vanish
  {
    echo "!! release-readiness not acceptable after $((n * 4))s (policy: ${mode}) — failing checks:"
    if [ -z "${body}" ]; then
      echo "   (no body — API down, or HTTP 403 = metrics token does not match the running backend)"
    else
      printf '%s' "${body}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for k, v in (d.get("checks") or {}).items():
    if isinstance(v, dict) and not v.get("ok"):
        print(f"   ✗ {k}: {json.dumps(v)[:400]}")
' 2>/dev/null || echo "   ${body:0:600}"
    fi
    echo "   full body: curl -sS -H \"X-Metrics-Token: \$(. deploy/lib.sh; metrics_token)\" http://127.0.0.1:8001/api/ops/release-readiness | python3 -m json.tool"
  } >&2
  return 1
}

# Confirm the RUNNING backend reports the SHA we just built (provenance proof).
verify_running_sha() {
  local running
  running=$(curl -fsS http://127.0.0.1:8001/api/health 2>/dev/null \
            | python3 -c 'import sys,json;print(json.load(sys.stdin).get("build_sha",""))' 2>/dev/null || true)
  if [ "${running}" != "${GIT_SHA}" ]; then
    echo "!! running build_sha (${running:-none}) != deployed ${GIT_SHA}"
    return 1
  fi
  echo "   running build_sha verified: ${running}"
}

# ── Release attestation gate (release review P1-2) ──────────────────────────
# Production deploys consume ONLY commits that carry a signed
# release-attestation (CI: SHA, image digests, test counts, scan results,
# gates, promotion decision). Verification = cosign keyless signature
# (Sigstore, identity pinned to this repo's workflows) + content gate.
# Controlled by ATTESTATION_REQUIRED in ./.env (default: true when the TLS
# production profile is active, false for --dev).
_repo_slug() {
  git remote get-url origin 2>/dev/null \
    | sed -E 's#^(git@github\.com:|https://github\.com/)##; s#\.git$##'
}

ensure_cosign() {
  command -v cosign >/dev/null 2>&1 && return 0
  echo "   installing cosign (release signature verification)"
  local ver=v2.5.3 arch
  arch=$(uname -m); case "$arch" in x86_64) arch=amd64;; aarch64) arch=arm64;; esac
  curl -sSfL -o /tmp/cosign "https://github.com/sigstore/cosign/releases/download/${ver}/cosign-linux-${arch}" \
    && sudo install -m 0755 /tmp/cosign /usr/local/bin/cosign
}

# DEPLOY_MODE in ./.env — auto (default, A18 Part 3): pull the signed CI images by attested digest when the
# ref is an attested release and the pull verifies; otherwise (or on a GHCR outage) build on the host.
# registry: pull only — never build (strict). build: always build locally (never pull).
deploy_mode() {
  local v
  v=$( { grep -E '^DEPLOY_MODE=' .env 2>/dev/null || true; } | cut -d= -f2- | tr -d "\"'")
  case "${v}" in registry) echo registry;; build) echo build;; *) echo auto;; esac
}

# The verified attestation on disk belongs to the commit being deployed (verify_attestation wrote it this run).
attestation_for_head() {
  [ -s release/attestation.current.json ] || return 1
  [ -n "${GIT_SHA:-}" ] || return 1
  [ "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("commit",""))' release/attestation.current.json 2>/dev/null)" = "${GIT_SHA}" ]
}

# auto mode can deploy from the registry when the attestation of THIS commit carries both image digests and the
# signed admission manifest was fetched with it.
registry_available() {
  [ "$(deploy_mode)" = "build" ] && return 1
  attestation_for_head || return 1
  [ -s release/attestation/release-admission.json ] || return 1
  python3 scripts/release_attestation.py verify --file release/attestation.current.json --sha "${GIT_SHA}" --require-images >/dev/null 2>&1
}

images_from_registry() {   # the last provision_images pulled the attested digests (deploy/state/deploy_source.json)
  [ "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("source",""))' deploy/state/deploy_source.json 2>/dev/null)" = registry ]
}

# deploy/state/deploy_source.json — what the running stack was provisioned from (registry | build | build-fallback);
# the backend shows it in release readiness (ro mount /app/state).
record_deploy_source() {   # record_deploy_source <source> <reason>
  mkdir -p deploy/state 2>/dev/null || return 0
  python3 - "$1" "$2" "${GIT_SHA:-}" "${STOIC_BACKEND_IMAGE:-}" "$(deploy_mode)" deploy/state/deploy_source.json <<'PY' || true
import json, os, sys
from datetime import datetime
src, reason, sha, img, mode, out = sys.argv[1:7]
doc = {"source": src, "reason": reason or None, "commit": sha or None, "backend_image": img or None, "deploy_mode": mode,
       "at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}
json.dump(doc, open(out + ".tmp", "w"), sort_keys=True); os.chmod(out + ".tmp", 0o644); os.replace(out + ".tmp", out)
PY
}

attestation_required() {
  # registry mode has no other source of truth for the digests — always required
  [ "$(deploy_mode)" = "registry" ] && return 0
  local v
  # N102-6 — an exported ATTESTATION_REQUIRED wins over the ./.env line (both are honoured)
  v="${ATTESTATION_REQUIRED:-}"
  [ -n "${v}" ] || v=$( { grep -E '^ATTESTATION_REQUIRED=' .env 2>/dev/null || true; } | cut -d= -f2- | tr -d "\"'")
  if [ -n "${v}" ]; then
    case "${v}" in
      true) return 0 ;;
      false)
        [ "$(app_env)" = "production" ] && echo "!! ATTESTATION_REQUIRED=false on a PRODUCTION host — release attestation gate consciously bypassed (remove the line to restore)"
        return 1 ;;
      *) echo "!! ATTESTATION_REQUIRED='${v}' is not true|false — treating as REQUIRED (fail closed)"; return 0 ;;
    esac
  fi
  [ "$(app_env)" = "production" ] && return 0
  grep -q 'docker-compose.tls.yml' .env 2>/dev/null
}

# r26 P1-01 — the packaged tree must be ONE provenance chain: BUILD_SHA, rc_lock
# source commit, model-manifest commit/digest and the TEST manifest digest must
# agree. Strict whenever attestation is required (or the lock is authoritative);
# a conscious --skip-attestation (ATTESTATION_REQUIRED=false) developer install
# still refuses digest mismatches unless STOIC_ALLOW_PROVENANCE_DRIFT=1.
verify_release_provenance() {
  [ -f scripts/release_consistency_check.py ] || { echo "!! scripts/release_consistency_check.py missing — provenance unverified"; return 1; }
  local strict=""; attestation_required && strict="--strict"
  if python3 scripts/release_consistency_check.py ${strict} "$@"; then return 0; fi
  if [ -z "${strict}" ] && [ "${STOIC_ALLOW_PROVENANCE_DRIFT:-0}" = 1 ]; then
    echo "!! release provenance INCONSISTENT — continuing only because STOIC_ALLOW_PROVENANCE_DRIFT=1 (developer snapshot, never releasable)"
    return 0
  fi
  echo "ERROR: release provenance inconsistent — the test manifest / source commit / model manifest / BUILD_SHA of this"
  echo "       tree do not bind to one release. Install a tagged release built by release.yml (one immutable staged tree)."
  return 1
}

verify_attestation() {
  resolve_git_sha || return 1
  rm -f release/attestation.current.json release/attestation.current.bundle deploy/state/release/attestation.current.json   # never reuse another commit's truth
  if ! attestation_required; then
    # A18 Part 3 — auto mode still LOOKS for the signed release: if it is there, the authoritative lock is adopted
    # and the images can be pulled by digest; if not, this stays a developer/on-host build (no gate).
    if [ "$(deploy_mode)" = "auto" ]; then
      if _verify_attestation_core; then return 0; fi
      echo "   attestation gate: not required — signed release not available for $(git rev-parse --short "${GIT_SHA}") (see above) → developer lock + on-host build"
      return 0
    fi
    echo "   attestation gate: not required (ATTESTATION_REQUIRED=false / dev)"; return 0
  fi
  _verify_attestation_core
}

_verify_attestation_core() {
  local repo tag dest=release/attestation
  repo=$(grep -E '^GITHUB_REPO=' .env 2>/dev/null | cut -d= -f2-); [ -n "${repo}" ] || repo=$(_repo_slug)
  tag=$(git tag --points-at "${GIT_SHA}" | grep -E '^v[0-9]' | head -1 || true)
  if [ -z "${tag}" ]; then
    echo "!! attestation gate: ${GIT_SHA} carries no v* release tag — production deploys only tagged, attested releases"
    return 1
  fi
  echo "-- attestation gate: ${tag} @ ${GIT_SHA} (repo ${repo})"
  rm -rf "${dest}"
  # GITHUB_TOKEN from ./.env is exported for private-repo asset downloads
  GITHUB_TOKEN="${GITHUB_TOKEN:-$(grep -E '^GITHUB_TOKEN=' .env 2>/dev/null | cut -d= -f2-)}" \
    python3 scripts/release_attestation.py fetch --repo "${repo}" --tag "${tag}" --dest "${dest}" || return 1
  ensure_cosign || { echo "!! cosign unavailable — cannot verify the release signature"; return 1; }
  # audit P2-2 — identity pinned to the EXACT release workflow at THIS tag
  # (not any workflow in the repo); the Rekor bundle is kept for offline DR verification.
  local ident="https://github.com/${repo}/.github/workflows/release.yml@refs/tags/${tag}"
  cosign verify-blob "${dest}/release-attestation.json" \
      --signature "${dest}/release-attestation.json.sig" \
      --certificate "${dest}/release-attestation.json.pem" \
      --certificate-identity "${ident}" \
      --certificate-github-workflow-repository "${repo}" \
      --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    || { echo "!! attestation SIGNATURE invalid or not issued by ${ident}"; return 1; }
  [ -f "${dest}/release-attestation.json.bundle" ] && cp "${dest}/release-attestation.json.bundle" release/attestation.current.bundle
  python3 scripts/release_attestation.py verify --file "${dest}/release-attestation.json" \
      --sha "${GIT_SHA}" --tag "${tag}" || return 1
  cp "${dest}/release-attestation.json" release/attestation.current.json
  publish_release_truth
  echo "   attestation gate: PASSED — recorded at release/attestation.current.json"
}

# The backend reads the host's release truth through the ro mount ./deploy/state:/app/state (a REGISTRY image
# cannot carry the lock that holds its own digest, so the copy baked into the image is never the authoritative one).
publish_release_truth() {
  mkdir -p deploy/state/release 2>/dev/null || return 0
  local f
  for f in release/attestation.current.json release/rc_lock.json; do
    [ -s "${f}" ] && cp "${f}" "deploy/state/release/$(basename "${f}")" && chmod 644 "deploy/state/release/$(basename "${f}")"
  done
  return 0
}

# ── Registry image deploys (DEPLOY_MODE=registry) ───────────────────────────
# Pull the EXACT CI-built images by the digests recorded in the verified
# attestation. Every digest is cosign-verified (keyless, identity pinned to
# this repo's workflows) BEFORE it is pulled, then pinned into ./.env for
# docker-compose.registry.yml. Nothing is built on the server.
ensure_registry_compose_file() {
  local cf
  cf=$( { grep -E '^COMPOSE_FILE=' .env 2>/dev/null || true; } | cut -d= -f2-); [ -n "${cf}" ] || cf=docker-compose.yml
  case ":${cf}:" in
    *:docker-compose.registry.yml:*) ;;
    *) set_kv .env COMPOSE_FILE "${cf}:docker-compose.registry.yml" ;;
  esac
}

disable_registry_compose_file() {   # on-host build: drop the registry overlay + pinned image refs from ./.env
  [ -f .env ] || return 0
  local cf new
  cf=$( { grep -E '^COMPOSE_FILE=' .env 2>/dev/null || true; } | cut -d= -f2-)
  case ":${cf}:" in
    *:docker-compose.registry.yml:*)
      new=$(printf '%s' "${cf}" | tr ':' '\n' | grep -vx 'docker-compose.registry.yml' | paste -sd: -)
      set_kv .env COMPOSE_FILE "${new:-docker-compose.yml}" ;;
  esac
  sed -i.bak '/^STOIC_BACKEND_IMAGE=/d;/^STOIC_FRONTEND_IMAGE=/d' .env && rm -f .env.bak
  unset STOIC_BACKEND_IMAGE STOIC_FRONTEND_IMAGE
}

registry_login() {
  local tok user
  tok="${GITHUB_TOKEN:-$(grep -E '^GITHUB_TOKEN=' .env 2>/dev/null | cut -d= -f2-)}"
  [ -n "${tok}" ] || { echo "   (no GITHUB_TOKEN in ./.env — pulling anonymously; private GHCR packages need a read:packages token, docs/DEPLOYMENT.md → Registry deploys)"; return 0; }
  user=$(grep -E '^GITHUB_REPO=' .env 2>/dev/null | cut -d= -f2- | cut -d/ -f1); [ -n "${user}" ] || user=$(_repo_slug | cut -d/ -f1)
  echo "${tok}" | docker login ghcr.io -u "${user:-stoic}" --password-stdin >/dev/null 2>&1 \
    || echo "   (ghcr.io login failed — continuing; public images still pull, private ones need a valid read:packages GITHUB_TOKEN)"
}

pull_attested_images() {
  local att=release/attestation.current.json repo be fe d id
  [ -f "${att}" ] || { echo "ERROR: registry mode needs the verified attestation (${att}) — run verify_attestation first"; return 1; }
  repo=$(grep -E '^GITHUB_REPO=' .env 2>/dev/null | cut -d= -f2-); [ -n "${repo}" ] || repo=$(_repo_slug)
  local chk
  chk=$(python3 scripts/release_attestation.py verify --file "${att}" --sha "${GIT_SHA}" --require-images 2>&1) \
    || { echo "${chk}"; return 1; }
  eval "$(python3 scripts/release_attestation.py images --file "${att}")" || return 1
  be="${ATT_BACKEND_IMAGE}"; fe="${ATT_FRONTEND_IMAGE}"
  local tag; tag=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("tag",""))' "${att}")
  [ -n "${tag}" ] || { echo "!! attestation carries no release tag"; return 1; }
  ensure_cosign || { echo "!! cosign unavailable — cannot verify image signatures"; return 1; }
  # r18 P2-01 — the signed PAIR admission manifest is mandatory: both digests must
  # match the attestation; a single tag/alias is never a deployable unit.
  local adm=release/attestation/release-admission.json
  [ -f "${adm}" ] || { echo "!! release-admission.json missing — refusing to deploy from tags/aliases"; return 1; }
  cosign verify-blob "${adm}" --signature "${adm}.sig" --certificate "${adm}.pem" \
      --certificate-identity "https://github.com/${repo}/.github/workflows/release.yml@refs/tags/${tag}" \
      --certificate-github-workflow-repository "${repo}" \
      --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    || { echo "!! admission manifest SIGNATURE invalid"; return 1; }
  python3 scripts/verify_admission.py "${adm}" --backend-digest "${be}" --frontend-digest "${fe}" --tag "${tag}" --commit "${GIT_SHA}" \
    || { echo "!! admission manifest does not match the attested digest pair"; return 1; }
  echo "   admission gate: PASSED — deploying the signed digest PAIR"
  registry_login
  for d in "${be}" "${fe}"; do
    echo "${d}" | grep -qE '^[a-z0-9./_-]+@sha256:[0-9a-f]{64}$' \
      || { echo "!! attested image reference is not digest-pinned: ${d}"; return 1; }
    cosign verify "${d}" \
        --certificate-identity "https://github.com/${repo}/.github/workflows/release.yml@refs/tags/${tag}" \
        --certificate-github-workflow-repository "${repo}" \
        --certificate-oidc-issuer https://token.actions.githubusercontent.com >/dev/null \
      || { echo "!! image SIGNATURE invalid or not issued by ${repo} workflows: ${d}"; return 1; }
    echo "   image signature verified: ${d}"
    docker pull -q "${d}" >/dev/null || { echo "!! pull failed: ${d} (GHCR outage, or a private package without a read:packages GITHUB_TOKEN in ./.env)"; return 1; }
    id=$(docker inspect --format '{{index .RepoDigests 0}}' "${d}" 2>/dev/null || true)
    [ "${id}" = "${d}" ] || { echo "!! pulled digest ${id:-none} != attested ${d}"; return 1; }
  done
  touch .env
  ensure_registry_compose_file
  set_kv .env STOIC_BACKEND_IMAGE "${be}"
  set_kv .env STOIC_FRONTEND_IMAGE "${fe}"
  set_kv .env STOIC_IMAGE_DIGEST "${be#*@}"
  set_kv .env GIT_SHA "${GIT_SHA}"
  export STOIC_BACKEND_IMAGE="${be}" STOIC_FRONTEND_IMAGE="${fe}" STOIC_IMAGE_DIGEST="${be#*@}"
  echo "   image provenance (registry): ${be#*@}"
}

# Make the images for the checked-out commit available.
#   registry : pull the attested digests — strict, never builds (an unverified tree must not become "the release")
#   build    : always build on the host
#   auto     : registry when this commit is an attested release and pull+verification succeed; otherwise build on the
#              host (logged + deploy/state/deploy_source.json → readiness `deploy_source`) — a GHCR outage never blocks
provision_images() {
  resolve_git_sha || return 1
  local mode; mode=$(deploy_mode)
  case "${mode}" in
    registry)
      echo "   deploy mode: registry — pulling CI-built images by attested digest (no local build)"
      pull_attested_images || return 1
      record_deploy_source registry "DEPLOY_MODE=registry" ;;
    build)
      echo "   deploy mode: build — building on this host (DEPLOY_MODE=build)"
      build_with_provenance || return 1
      record_deploy_source build "DEPLOY_MODE=build" ;;
    *)
      if registry_available; then
        echo "   deploy mode: auto — attested release found: pulling CI-built images by digest"
        if pull_attested_images; then
          record_deploy_source registry "attested release $(git rev-parse --short "${GIT_SHA}")"
        else
          echo "!! registry pull/verification failed — falling back to an on-host build of the same commit (deploy/state/deploy_source.json records it; the LIVE digest gate stays red until a registry deploy succeeds)"
          echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD) registry-fallback-build sha=${GIT_SHA}" >> deploy/releases.log
          build_with_provenance || return 1
          record_deploy_source build-fallback "registry pull/verification failed"
        fi
      else
        echo "   deploy mode: auto — no attested images for $(git rev-parse --short "${GIT_SHA}") (untagged ref, attestation unavailable or ATTESTATION_REQUIRED=false without a release) → building on this host"
        build_with_provenance || return 1
        record_deploy_source build "no attested images for this ref"
      fi ;;
  esac
}

# `docker compose up` for the active deploy mode — registry mode must never
# fall back to a local build of an unverified tree.
# Containers compose can no longer manage: "removing"/"dead" ones, plus the
# `<12hex>_<project>-<service>-N` leftovers compose renames the old container to
# while recreating — when the removal fails (overlay EBUSY) they keep the name
# and every later `compose up` dies with "container name already in use".
zombie_ids() {
  local project; project=$(basename "$PWD")
  { docker compose ps -aq --status removing --status dead 2>/dev/null || true
    docker ps -aq --filter "status=dead" --filter "status=removing" --filter "name=${project}-" 2>/dev/null || true
    docker ps -a --format '{{.ID}} {{.Names}}' 2>/dev/null | grep -E " [0-9a-f]{12}_${project}-" | cut -d' ' -f1 || true
  } | sort -u | tr '\n' ' '
}

# Services that run in their own mount namespace (systemd PrivateTmp, cPanel
# CageFS/LVE php-fpm pools, node apps) receive a COPY of every container rootfs
# mount that existed when they started. Those copies pin the merged dir →
# docker rm fails "unlinkat …/merged: device or resource busy". Detach the copies
# (lazily, inside each foreign namespace) — they are dead weight there. Only
# private/slave copies are touched: a `shared:` peer would propagate the umount
# back to the host and kill a live container, so those are reported instead.
detach_leaked_mounts() {
  command -v nsenter >/dev/null || return 0
  local proc="${PROC_ROOT:-/proc}" host_ns; host_ns=$(readlink "${proc}/1/ns/mnt" 2>/dev/null) || return 0
  local seen=" " p ns pid n=0 shared=0 failed=0 first_err="" live
  # rootfs of RUNNING containers: a shared-peer copy of those must not be touched (umount would
  # propagate back to the host and kill the container); dead containers' copies are fair game
  live=" $(docker ps -q --no-trunc 2>/dev/null | tr '\n' ' ')$(docker ps -q 2>/dev/null | xargs -r docker inspect -f '{{.GraphDriver.Data.MergedDir}}' 2>/dev/null | tr '\n' ' ') "
  for p in "${proc}"/[0-9]*; do
    ns=$(readlink "$p/ns/mnt" 2>/dev/null) || continue
    [ "${ns}" = "${host_ns}" ] && continue
    case "${seen}" in *" ${ns} "*) continue ;; esac
    grep -qs '/var/lib/docker/' "$p/mountinfo" || continue
    grep -qs 'docker\|containerd' "$p/cgroup" && continue      # a container's own namespace — leave it
    seen="${seen}${ns} "; pid=${p##*/}
    # field 5 = mount point; optional fields (7 … up to the "-" separator) carry shared:/master: tags
    while read -r mp tag; do
      if [ "${tag}" = shared ]; then
        case "${mp}" in */containers/*) cid=${mp#*/containers/}; cid=${cid%%/*} ;; *) cid="${mp}" ;; esac
        case "${live}" in *" ${cid} "*|*" ${mp} "*) shared=$((shared+1)); continue ;; esac
      fi
      if err=$(nsenter -m -t "${pid}" -- umount -l "${mp}" 2>&1); then n=$((n+1))
      else failed=$((failed+1)); [ -n "${first_err}" ] || first_err="pid ${pid} ($(cat "$p/comm" 2>/dev/null)): ${err}"; fi
    done < <(awk '$5 ~ "^/var/lib/docker/" { t=""; for (i=7; i<=NF && $i!="-"; i++) t=t" "$i; print $5, (t ~ /shared:/ ? "shared" : "private") }' "$p/mountinfo" 2>/dev/null)
  done
  [ "${n}" -gt 0 ] && echo "-- detached ${n} leaked docker mount copies from $(( $(echo "${seen}" | wc -w) )) foreign mount namespaces (php-fpm/PrivateTmp services)"
  [ "${failed}" -gt 0 ] && echo "!! ${failed} leaked copies could not be detached — first error: ${first_err}"
  [ "${shared}" -gt 0 ] && echo "!! ${shared} leaked copies are shared peers of RUNNING containers' mounts — left alone (deploy/doctor.sh → 'docker mount propagation')"
  return 0
}

# RHEL 8 overlay2 leaves containers "marked for removal" (device or resource
# busy — a mount leaked into another mount namespace: cPanel CageFS/LVE, httpd
# PrivateTmp) unless fs.may_detach_mounts=1. Force-remove, escalating to a
# dockerd restart and finally to metadata removal with dockerd stopped.
#
# r26 P2-03 safety envelope — host mutation is OPT-IN:
#   * default = DIAGNOSIS ONLY: report zombies / leaked copies and refuse to continue
#   * STOIC_REPAIR_DOCKER_MOUNTS=1 (--repair-docker-mounts) enables namespace detach,
#     rm -f, dockerd restart and metadata removal
#   * every ID is re-resolved to its FULL 64-hex ID immediately before deletion, must be
#     dead/removing (or a compose-renamed leftover) AND carry this compose project's label;
#     short/ambiguous IDs and foreign containers are refused
#   * container metadata is snapshotted before removal; every mutation is appended to a
#     hash-chained repair journal (deploy/releases/docker-repair-journal.jsonl) that the
#     signed install report digests
wait_docker() {   # dockerd restores containers before answering — after a metadata cleanup this can take a while
  local i; for i in $(seq 1 45); do docker info >/dev/null 2>&1 && return 0; sleep 2; done; return 1
}

# Opt-in via env (one run) or persisted in .env (hosts with the known overlay2
# mount-propagation problem, where every recreate otherwise needs a repair).
repair_enabled() {
  [ "${STOIC_REPAIR_DOCKER_MOUNTS:-0}" = 1 ] && return 0
  [ "$( { grep -E '^STOIC_REPAIR_DOCKER_MOUNTS=' .env 2>/dev/null || true; } | cut -d= -f2- | tr -d '"' )" = 1 ]
}

REPAIR_JOURNAL="${STOIC_REPAIR_JOURNAL:-deploy/releases/docker-repair-journal.jsonl}"
repair_journal() {   # repair_journal <action> <detail…>  — hash-chained JSON line (prev_hash → entry_hash)
  mkdir -p "$(dirname "${REPAIR_JOURNAL}")"
  ACTION="$1" DETAIL="${*:2}" JOURNAL="${REPAIR_JOURNAL}" python3 - <<'PY' 2>/dev/null || true
import hashlib, json, os, socket, time
j = os.environ["JOURNAL"]; prev = "0" * 64; lines = []
try:
    lines = [l for l in open(j).read().splitlines() if l.strip()]
    if lines: prev = json.loads(lines[-1])["entry_hash"]
except (OSError, ValueError, KeyError): pass
e = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "host": socket.gethostname(),
     "action": os.environ["ACTION"], "detail": os.environ["DETAIL"], "prev_hash": prev}
e["entry_hash"] = hashlib.sha256(json.dumps(e, sort_keys=True).encode()).hexdigest()
open(j, "a").write(json.dumps(e, sort_keys=True) + "\n")
summary = {"journal": j, "entries": len(lines) + 1, "last_entry_hash": e["entry_hash"]}
json.dump(summary, open(os.path.join(os.path.dirname(j), "docker_repair_summary.json"), "w"), sort_keys=True)
PY
}

# resolve_zombie <id-or-name> → prints "<full64hexid> <status>" when the container is dead/removing
# (or a compose-renamed leftover) AND belongs to this compose project; prints nothing otherwise.
resolve_zombie() {
  local ref="$1" project; project=$(basename "$PWD")
  [ "${#ref}" -ge 12 ] || return 0                                   # refuse short / ambiguous references
  local full status name label
  full=$(docker inspect -f '{{.Id}}' "${ref}" 2>/dev/null || true)
  if [ -z "${full}" ]; then                                         # dockerd stopped or metadata-only: exact dir match
    local dirs; dirs=$(ls -d /var/lib/docker/containers/"${ref}"* 2>/dev/null | wc -l)
    [ "${dirs}" = 1 ] || return 0
    full=$(basename "$(ls -d /var/lib/docker/containers/"${ref}"* 2>/dev/null)")
    [[ "${full}" =~ ^[0-9a-f]{64}$ ]] || return 0
    python3 - "${full}" "${project}" <<'PY' && return 0 || return 0
import json, sys
full, project = sys.argv[1:]
try:
    cfg = json.load(open(f"/var/lib/docker/containers/{full}/config.v2.json"))
except (OSError, ValueError):
    sys.exit(1)
labels = cfg.get("Config", {}).get("Labels") or {}
if labels.get("com.docker.compose.project") != project:
    sys.exit(1)
st = cfg.get("State", {})
if st.get("Running") or st.get("Paused") or st.get("Restarting"):
    sys.exit(1)
print(full, "dead" if st.get("Dead") else ("removing" if st.get("RemovalInProgress") else "stopped"))
PY
  fi
  [[ "${full}" =~ ^[0-9a-f]{64}$ ]] || return 0
  status=$(docker inspect -f '{{.State.Status}}' "${full}" 2>/dev/null || true)
  name=$(docker inspect -f '{{.Name}}' "${full}" 2>/dev/null | sed 's#^/##' || true)
  label=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "${full}" 2>/dev/null || true)
  [ "${label}" = "${project}" ] || return 0                          # never touch a foreign container
  case "${status}" in
    dead|removing) echo "${full} ${status}" ;;
    exited|created) case "${name}" in [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]_"${project}"-*) echo "${full} ${status}" ;; esac ;;
  esac
}

snapshot_container_metadata() {   # metadata only (config/hostconfig json) — tiny, restorable
  local dest="deploy/releases/docker-repair-snapshots/$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "${dest}"
  local z; for z in "$@"; do
    [ -d "/var/lib/docker/containers/${z}" ] && tar czf "${dest}/${z:0:12}.tgz" -C /var/lib/docker/containers "${z}" --exclude='*-json.log' --exclude='mounts' 2>/dev/null || true
  done
  echo "${dest}"
}

diagnose_zombies() {   # read-only picture used by the default (no-repair) path
  local zombies; zombies=$(zombie_ids)
  [ -n "${zombies// /}" ] || return 0
  echo "!! stale containers from a previous run: ${zombies}"
  local z r; for z in ${zombies}; do
    r=$(resolve_zombie "$z"); [ -n "$r" ] && echo "   would remove ${r}" || echo "   would REFUSE ${z} (not this project / not dead / ambiguous)"
  done
  echo "!! docker mount recovery is diagnosis-only by default. Re-run with --repair-docker-mounts"
  echo "   (STOIC_REPAIR_DOCKER_MOUNTS=1) to detach leaked mount copies, remove the containers above and,"
  echo "   as a last resort, drop their metadata with dockerd stopped. Every mutation is journaled in ${REPAIR_JOURNAL}."
  return 1
}

# cPanel VirtFS (jailed shells) bind-mounts /var/lib into /home/virtfs/<user>/ and so
# copies every container rootfs mount INTO THE HOST NAMESPACE. A copy shares the merged
# dir's dentry: after the container dies `rmdir merged` → EBUSY even on 4.18 kernels
# (is_local_mountpoint). Only COPIES (path ≠ docker root) of layers NO container owns
# are touched, never shared peers (their umount would propagate back and kill a live
# container), and never with dockerd down (every layer would look orphaned).
orphan_merged_copies() {   # stdout: "<layer-id> <mount-path>" for each detachable copy
  local live
  live=" $(docker ps -aq --no-trunc 2>/dev/null | xargs -r docker inspect -f '{{.GraphDriver.Data.MergedDir}}' 2>/dev/null | sed 's|.*/overlay2/||; s|/merged$||' | tr '\n' ' ') " || return 0
  docker info >/dev/null 2>&1 || return 0
  awk '$5 ~ /\/var\/lib\/docker\/overlay2\/[^\/]+\/merged$/ && $5 !~ /^\/var\/lib\/docker\// {
         sh = 0; for (i = 7; i <= NF && $i != "-"; i++) if ($i ~ /^shared:/) sh = 1
         if (!sh) { n = split($5, a, "/"); print a[n-1], $5 } }' "${PROC_ROOT:-/proc}/1/mountinfo" 2>/dev/null \
    | while read -r id p; do case "${live}" in *" ${id} "*) ;; *) echo "${id} ${p}" ;; esac; done
}
detach_orphan_copies() {
  local n=0 id p
  while read -r id p; do [ -n "${p}" ] && umount -l "${p}" 2>/dev/null && n=$((n+1)); done < <(orphan_merged_copies)
  [ "${n}" -gt 0 ] && { echo "-- detached ${n} host-namespace copies of dead containers' rootfs mounts (cPanel VirtFS binds of /var/lib)"; repair_journal detach_orphan_copies "virtfs copies=${n}"; }
  return 0
}

reap_zombies() {
  if ! repair_enabled; then diagnose_zombies; return $?; fi
  detach_orphan_copies
  detach_leaked_mounts
  local zombies; zombies=$(zombie_ids)
  [ -n "${zombies// /}" ] || return 0
  repair_journal detach_leaked_mounts "foreign-namespace umount -l sweep before reaping: ${zombies}"
  # re-resolve each reference to a full, project-owned, dead/removing ID — refuse the rest
  local targets="" z r refused=""
  for z in ${zombies}; do r=$(resolve_zombie "$z"); [ -n "$r" ] && targets="${targets} ${r%% *}" || refused="${refused} ${z}"; done
  [ -n "${refused// /}" ] && echo "!! refusing to touch: ${refused} (not this compose project / not dead / ambiguous id)"
  [ -n "${targets// /}" ] || return 0
  echo "-- removing stale containers left from a previous run: ${targets}"
  local snap; snap=$(snapshot_container_metadata ${targets}); repair_journal snapshot "${snap} ids=${targets}"
  [ "$(cat /proc/sys/fs/may_detach_mounts 2>/dev/null || echo 1)" = 1 ] \
    || { echo "-- fs.may_detach_mounts=0 — enabling so leaked overlay mounts can be detached"; sysctl -qw fs.may_detach_mounts=1 2>/dev/null || true; repair_journal sysctl "fs.may_detach_mounts=1"; }
  for z in ${targets}; do   # overlay "device or resource busy": lazily unmount the merged dir first — and every VirtFS copy of it
    m=$(docker inspect -f '{{.GraphDriver.Data.MergedDir}}' "$z" 2>/dev/null || true); [ -n "$m" ] || continue
    for p in $(awk -v id="$(basename "$(dirname "$m")")" '$5 ~ ("/overlay2/" id "/merged$") {print $5}' /proc/1/mountinfo 2>/dev/null) "$m"; do umount -l "$p" 2>/dev/null || true; done
  done
  if ! docker rm -f ${targets} >/dev/null 2>&1 || [ -n "$(zombie_ids | tr -d ' ')" ]; then
    repair_journal docker_rm_failed "ids=${targets}"
    systemctl reset-failed docker docker.socket 2>/dev/null || true   # several restarts within a minute trip systemd's start-limit
    systemctl restart docker 2>/dev/null || true; wait_docker || true; repair_journal dockerd_restart "after rm -f failure"
    docker rm -f ${targets} >/dev/null 2>&1 || true
  fi
  repair_journal docker_rm "ids=${targets}"
  # last resort: drop the container metadata while dockerd is stopped — re-resolved, exact full-ID directories only
  local left="" merged=""
  for z in $(zombie_ids); do r=$(resolve_zombie "$z"); [ -n "$r" ] && left="${left} ${r%% *}"; done
  if [ -n "${left// /}" ]; then
    echo "-- stale containers survived rm -f; removing their metadata with dockerd stopped: ${left}"
    for z in ${left}; do merged="${merged} $(docker inspect -f '{{.GraphDriver.Data.MergedDir}}' "$z" 2>/dev/null || true)"; done
    systemctl stop docker docker.socket 2>/dev/null || true; repair_journal dockerd_stop "metadata removal ids=${left}"
    for m in ${merged}; do umount -l "$m" 2>/dev/null || true; done
    for z in ${left}; do
      [[ "${z}" =~ ^[0-9a-f]{64}$ ]] || { echo "!! refusing metadata removal for non-full id ${z}"; continue; }
      r=$(resolve_zombie "$z"); [ -n "$r" ] || { echo "!! refusing metadata removal for ${z:0:12} (re-validation failed)"; continue; }
      [ -d "/var/lib/docker/containers/${z}" ] && { rm -rf "/var/lib/docker/containers/${z}"; repair_journal metadata_removed "${z}"; }
    done
    systemctl reset-failed docker docker.socket 2>/dev/null || true
    systemctl start docker 2>/dev/null || true
    wait_docker || { repair_journal dockerd_start_failed "after metadata removal"; echo "ERROR: dockerd did not come back within 90s after zombie cleanup — journalctl -u docker -n 50"; journalctl -u docker -n 20 --no-pager 2>/dev/null || true; return 1; }
    repair_journal dockerd_start "ok"
  fi
}

# `docker compose up` for the active deploy mode — registry mode must never
# fall back to a local build of an unverified tree. On hosts where every
# container's overlay mount is held busy (cPanel), each `up` pass fails on the
# NEXT container it recreates — reap and retry until compose converges.
# The containers compose is about to recreate are still alive when reap_zombies runs, so
# their VirtFS copies are not "orphan" yet — and `docker rm` hits them seconds later.
# Drop the COPIES (never the original mount) for this project's containers up front: the
# copies are private, so the running container is unaffected and the jail never uses them.
detach_project_copies() {
  repair_enabled || return 0
  local n=0 c m id p
  for c in $(docker compose ps -aq 2>/dev/null); do
    m=$(docker inspect -f '{{.GraphDriver.Data.MergedDir}}' "$c" 2>/dev/null) || continue; [ -n "$m" ] || continue
    id=$(basename "$(dirname "$m")")
    for p in $(awk -v id="$id" '$5 ~ ("/overlay2/" id "/merged$") && $5 !~ /^\/var\/lib\/docker\// {
                 sh = 0; for (i = 7; i <= NF && $i != "-"; i++) if ($i ~ /^shared:/) sh = 1; if (!sh) print $5 }' /proc/1/mountinfo 2>/dev/null); do
      umount -l "$p" 2>/dev/null && n=$((n+1))
    done
  done
  [ "$n" -gt 0 ] && echo "-- detached ${n} VirtFS copies of this project's container rootfs mounts ahead of recreate"
  return 0
}

compose_up() {
  reap_zombies || return 1
  detach_project_copies
  local flags="-d --remove-orphans" attempt pass_started
  # M120-2 — never rebuild over pulled digests: strict registry mode, or auto mode after a successful registry pull
  { [ "$(deploy_mode)" = "registry" ] || images_from_registry; } && flags="${flags} --no-build"
  for attempt in 1 2 3 4 5 6; do
    pass_started=$(date -u +%s)
    docker compose up ${flags} "$@" && return 0
    if app_boot_failure "${pass_started}"; then
      echo "ERROR: the new build's container(s) failed to boot (see log above) — this is not a Docker mount problem; fix the cause and re-run"
      return 1
    fi
    [ "${attempt}" = 6 ] && break
    echo "-- compose up failed (attempt ${attempt}/6) — reaping containers that died during recreate and retrying"
    reap_zombies || return 1
  done
  echo "-- final state of the stack (last 40 backend log lines follow):"
  docker compose ps -a 2>/dev/null || true
  docker compose logs --tail 40 backend 2>/dev/null | sed 's/^/   | /' || true
  echo "ERROR: compose up did not converge after 6 attempts — run deploy/doctor.sh (section 'docker mount propagation') to see which host processes hold the overlay mounts$(repair_enabled || echo '; docker mount recovery is diagnosis-only — re-run with --repair-docker-mounts to let the installer repair')"
  return 1
}
