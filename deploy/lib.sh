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
#   order_auth_secret / ledger_anchor_key : signing keys distinct from JWT (boot rule)
#   secrets_master_key                    : dedicated integrations-vault key (audit r28 P2-02);
#                                           legacy JWT-derived records are migrated at boot
ensure_release_secrets() {
  [ -d secrets ] || return 0
  local f
  # create with mode 0600 from the start (no umask window), regenerate zero-byte leftovers
  for f in order_auth_secret ledger_anchor_key; do
    [ -s "secrets/${f}" ] || { (umask 077; python3 -c "import secrets;print(secrets.token_urlsafe(32))" > "secrets/${f}"); echo "   generated ${f}"; }
  done
  [ -s secrets/secrets_master_key ] || {
    (umask 077; python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())" > secrets/secrets_master_key)
    echo "   generated secrets_master_key (dedicated vault key — sealed integration secrets migrate at boot)"
  }
  # N-R2 — every `file:` secret docker-compose*.yml mounts must exist BEFORE compose up, also
  # on hosts installed before the secret was introduced (update.sh path). Empty placeholder:
  # the operator pastes the BotFather token to enable security alerts.
  [ -f secrets/security_telegram_token ] || { (umask 077; : > secrets/security_telegram_token); echo "   created empty security_telegram_token (paste the BotFather token to enable security alerts)"; }
  chmod 600 secrets/order_auth_secret secrets/ledger_anchor_key secrets/secrets_master_key secrets/security_telegram_token
}

# After a failed `compose up`, an app container CREATED BY THIS PASS that exited
# or is unhealthy is a boot failure of the new build — not a mount leak. Print its
# log and stop retrying so the cause is visible instead of buried under 6 identical
# attempts. Containers older than the pass (previous build, mid-recreate) are ignored.
app_boot_failure() {
  local since="$1" c created state found=1
  for c in $(docker compose ps -a --format '{{.Name}} {{.State}} {{.Health}}' 2>/dev/null \
             | awk '$1 ~ /-(backend|worker-[a-z]+|frontend)-[0-9]+$/ && ($2 == "exited" || $3 == "unhealthy") {print $1}'); do
    created=$(date -u -d "$(docker inspect -f '{{.Created}}' "$c" 2>/dev/null)" +%s 2>/dev/null || echo 0)
    [ "${created}" -ge "${since}" ] || continue
    found=0
    state=$(docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "$c" 2>/dev/null || true)
    echo "!! ${c}: ${state} — last log lines:"
    docker logs --tail 60 "$c" 2>&1 | sed 's/^/   | /'
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

# build (default): rebuild images locally from the checkout.
# registry: pull the exact CI-built GHCR images by attested digest (no rebuild).
deploy_mode() {
  local v
  v=$( { grep -E '^DEPLOY_MODE=' .env 2>/dev/null || true; } | cut -d= -f2-)
  case "${v}" in registry) echo registry;; *) echo build;; esac
}

attestation_required() {
  # registry mode has no other source of truth for the digests — always required
  [ "$(deploy_mode)" = "registry" ] && return 0
  local v
  v=$( { grep -E '^ATTESTATION_REQUIRED=' .env 2>/dev/null || true; } | cut -d= -f2-)
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
  if ! attestation_required; then
    echo "   attestation gate: not required (ATTESTATION_REQUIRED=false / dev)"; return 0
  fi
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
  echo "   attestation gate: PASSED — recorded at release/attestation.current.json"
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

registry_login() {
  local tok user
  tok="${GITHUB_TOKEN:-$(grep -E '^GITHUB_TOKEN=' .env 2>/dev/null | cut -d= -f2-)}"
  [ -n "${tok}" ] || return 0   # public packages need no login
  user=$(grep -E '^GITHUB_REPO=' .env 2>/dev/null | cut -d= -f2- | cut -d/ -f1); [ -n "${user}" ] || user=$(_repo_slug | cut -d/ -f1)
  echo "${tok}" | docker login ghcr.io -u "${user:-stoic}" --password-stdin >/dev/null 2>&1 \
    || echo "   (ghcr.io login failed — continuing; public images still pull)"
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
    docker pull -q "${d}" >/dev/null || { echo "!! pull failed: ${d}"; return 1; }
    id=$(docker inspect --format '{{index .RepoDigests 0}}' "${d}" 2>/dev/null || true)
    [ "${id}" = "${d}" ] || { echo "!! pulled digest ${id:-none} != attested ${d}"; return 1; }
  done
  touch .env
  ensure_registry_compose_file
  set_kv .env DEPLOY_MODE registry
  set_kv .env STOIC_BACKEND_IMAGE "${be}"
  set_kv .env STOIC_FRONTEND_IMAGE "${fe}"
  set_kv .env STOIC_IMAGE_DIGEST "${be#*@}"
  set_kv .env GIT_SHA "${GIT_SHA}"
  export STOIC_BACKEND_IMAGE="${be}" STOIC_FRONTEND_IMAGE="${fe}" STOIC_IMAGE_DIGEST="${be#*@}"
  echo "   image provenance (registry): ${be#*@}"
}

# Make the images for the checked-out commit available: build locally
# (default) or pull the attested digests (DEPLOY_MODE=registry).
provision_images() {
  resolve_git_sha || return 1
  if [ "$(deploy_mode)" = "registry" ]; then
    echo "   deploy mode: registry — pulling CI-built images by attested digest (no local build)"
    pull_attested_images
  else
    build_with_provenance
  fi
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
  [ "$(deploy_mode)" = "registry" ] && flags="${flags} --no-build"
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
  echo "ERROR: compose up did not converge after 6 attempts — run deploy/doctor.sh (section 'docker mount propagation') to see which host processes hold the overlay mounts$(repair_enabled || echo '; docker mount recovery is diagnosis-only — re-run with --repair-docker-mounts to let the installer repair')"
  return 1
}
