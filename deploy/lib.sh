#!/usr/bin/env bash
# Shared helpers for deploy/install.sh · update.sh · rollback.sh
# Source from the repo root:  . deploy/lib.sh

# Set-or-append KEY=VALUE in a dotenv file (idempotent).
set_kv() {
  if grep -q "^$2=" "$1" 2>/dev/null; then
    sed -i.bak "s|^$2=.*|$2=$3|" "$1" && rm -f "$1.bak"
  else
    printf '%s=%s\n' "$2" "$3" >> "$1"
  fi
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
  docker compose build
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
  if [ -f secrets/metrics_token ]; then cat secrets/metrics_token
  else grep -E '^METRICS_TOKEN=' backend/.env 2>/dev/null | cut -d= -f2- | tr -d '"'
  fi
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

# Wait until the FULL topology readiness probe is green (workers need ~45s
# to acquire leases). Prints the body on success; returns 1 on timeout.
wait_release_ready() {
  local n="${1:-45}" tok body i
  tok=$(metrics_token)
  [ -n "${tok}" ] || { echo "ERROR: metrics token missing (secrets/metrics_token or backend/.env)"; return 1; }
  for i in $(seq 1 "$n"); do
    if body=$(curl -fsS -H "X-Metrics-Token: ${tok}" http://127.0.0.1:8001/api/ops/release-readiness 2>/dev/null); then
      echo "${body}"; return 0
    fi
    sleep 4
  done
  echo "!! release-readiness never returned ready — last body:"
  curl -sS -H "X-Metrics-Token: ${tok}" http://127.0.0.1:8001/api/ops/release-readiness || true
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
  v=$(grep -E '^DEPLOY_MODE=' .env 2>/dev/null | cut -d= -f2-)
  case "${v}" in registry) echo registry;; *) echo build;; esac
}

attestation_required() {
  # registry mode has no other source of truth for the digests — always required
  [ "$(deploy_mode)" = "registry" ] && return 0
  local v
  v=$(grep -E '^ATTESTATION_REQUIRED=' .env 2>/dev/null | cut -d= -f2-)
  if [ -n "${v}" ]; then [ "${v}" = "true" ]; return; fi
  grep -q 'docker-compose.tls.yml' .env 2>/dev/null
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
  cf=$(grep -E '^COMPOSE_FILE=' .env 2>/dev/null | cut -d= -f2-); [ -n "${cf}" ] || cf=docker-compose.yml
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

# RHEL 8 overlay2 leaves containers "marked for removal" (device or resource
# busy — a mount leaked into another mount namespace: cPanel CageFS/LVE, httpd
# PrivateTmp) unless fs.may_detach_mounts=1. Force-remove, escalating to a
# dockerd restart and finally to metadata removal with dockerd stopped.
reap_zombies() {
  local zombies; zombies=$(zombie_ids)
  [ -n "${zombies// /}" ] || return 0
  echo "-- removing stale containers left from a previous run: ${zombies}"
  [ "$(cat /proc/sys/fs/may_detach_mounts 2>/dev/null || echo 1)" = 1 ] \
    || { echo "-- fs.may_detach_mounts=0 — enabling so leaked overlay mounts can be detached"; sysctl -qw fs.may_detach_mounts=1 2>/dev/null || true; }
  for z in ${zombies}; do   # overlay "device or resource busy": lazily unmount the merged dir first
    m=$(docker inspect -f '{{.GraphDriver.Data.MergedDir}}' "$z" 2>/dev/null || true); [ -n "$m" ] && umount -l "$m" 2>/dev/null || true
  done
  if ! docker rm -f ${zombies} >/dev/null 2>&1 || [ -n "$(zombie_ids | tr -d ' ')" ]; then
    systemctl restart docker 2>/dev/null || true; sleep 5
    docker rm -f $(zombie_ids) >/dev/null 2>&1 || true
  fi
  # last resort: drop the container metadata while dockerd is stopped — only the zombie IDs, nothing else
  local left; left=$(zombie_ids)
  if [ -n "${left// /}" ]; then
    echo "-- stale containers survived rm -f; removing their metadata with dockerd stopped"
    local merged=""; for z in ${left}; do merged="${merged} $(docker inspect -f '{{.GraphDriver.Data.MergedDir}}' "$z" 2>/dev/null || true)"; done
    systemctl stop docker docker.socket 2>/dev/null || true
    for m in ${merged}; do umount -l "$m" 2>/dev/null || true; done
    for z in ${left}; do
      full=$(ls -d /var/lib/docker/containers/"$z"* 2>/dev/null | head -1 || true)
      [ -n "$full" ] && rm -rf "$full"
    done
    systemctl start docker 2>/dev/null || true; sleep 5
    docker info >/dev/null 2>&1 || { echo "ERROR: dockerd did not come back after zombie cleanup"; return 1; }
  fi
}

# `docker compose up` for the active deploy mode — registry mode must never
# fall back to a local build of an unverified tree. Containers that die WHILE
# compose recreates them (the EBUSY case above) are reaped and `up` retried once.
compose_up() {
  reap_zombies || return 1
  local flags="-d --remove-orphans"
  [ "$(deploy_mode)" = "registry" ] && flags="${flags} --no-build"
  if ! docker compose up ${flags} "$@"; then
    echo "-- compose up failed — reaping containers that died during recreate and retrying once"
    reap_zombies || return 1
    docker compose up ${flags} "$@"
  fi
}
