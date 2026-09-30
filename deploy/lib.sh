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
wait_release_ready() {
  local n="${1:-45}" tok body i
  tok=$(metrics_token)
  [ -n "${tok}" ] || { echo "ERROR: metrics token missing (secrets/metrics_token or backend/.env)" >&2; return 1; }
  for i in $(seq 1 "$n"); do
    if body=$(curl -fsS -H "X-Metrics-Token: ${tok}" http://127.0.0.1:8001/api/ops/release-readiness 2>/dev/null); then
      echo "${body}"; return 0
    fi
    sleep 4
  done
  # callers capture stdout — diagnostics MUST go to stderr or they vanish
  {
    echo "!! release-readiness never returned ready ($((n * 4))s) — failing checks:"
    curl -sS -H "X-Metrics-Token: ${tok}" http://127.0.0.1:8001/api/ops/release-readiness 2>/dev/null \
      | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("   (no JSON body — is the API up? HTTP 403 = wrong metrics token)"); sys.exit(0)
for k, v in (d.get("checks") or {}).items():
    if isinstance(v, dict) and not v.get("ok"):
        print(f"   ✗ {k}: {json.dumps(v)[:400]}")
' || true
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

repair_enabled() { [ "${STOIC_REPAIR_DOCKER_MOUNTS:-0}" = 1 ]; }

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

reap_zombies() {
  if ! repair_enabled; then diagnose_zombies; return $?; fi
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
  for z in ${targets}; do   # overlay "device or resource busy": lazily unmount the merged dir first
    m=$(docker inspect -f '{{.GraphDriver.Data.MergedDir}}' "$z" 2>/dev/null || true); [ -n "$m" ] && { umount -l "$m" 2>/dev/null || true; }
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
compose_up() {
  reap_zombies || return 1
  local flags="-d --remove-orphans" attempt
  [ "$(deploy_mode)" = "registry" ] && flags="${flags} --no-build"
  for attempt in 1 2 3 4 5 6; do
    docker compose up ${flags} "$@" && return 0
    [ "${attempt}" = 6 ] && break
    echo "-- compose up failed (attempt ${attempt}/6) — reaping containers that died during recreate and retrying"
    reap_zombies || return 1
  done
  echo "ERROR: compose up did not converge after 6 attempts — run deploy/doctor.sh (section 'docker mount propagation') to see which host processes hold the overlay mounts$(repair_enabled || echo '; docker mount recovery is diagnosis-only — re-run with --repair-docker-mounts to let the installer repair')"
  return 1
}
