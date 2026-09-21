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
