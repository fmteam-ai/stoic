#!/usr/bin/env bash
# Preflight shared by deploy/update.sh and deploy/restart.sh — sourced AFTER deploy/lib.sh.
# Runs BEFORE anything is built, pulled or recreated:
#   1. host prerequisites (deploy/host-prereqs.sh): fs.may_detach_mounts=1 + docker root slave —
#      applied automatically (logged) or refused with the one-line command under --no-host-changes
#   2. leftovers of a previous jam: detach orphan overlay copies, remove dead/removing/created
#      containers and compose's "<12hex>_<project>-*" rename leftovers. Volumes are NEVER touched.
#   3. compose_up_guarded: one `up -d`; on an overlay EBUSY recreate failure clean once more and retry
#      ONCE; then stop with the reboot recipe instead of leaving a half-recreated stack.
#   4. CI release public key pin (RELEASE_PUBLIC_KEY_B64) fetched from the public signer — never the
#      local runtime sidecar key (N102-5).
#   5. shared web host detection (cPanel/WHM, Plesk, DirectAdmin, httpd/exim/dovecot) → backend/.env
#      STOIC_HOST_PROFILE so readiness can warn/block.
# Every host mutation prints what it changed; re-running changes nothing the second time.

PREFLIGHT_NO_HOST_CHANGES="${PREFLIGHT_NO_HOST_CHANGES:-0}"
PREFLIGHT_YES="${PREFLIGHT_YES:-0}"
EBUSY_RE='device or resource busy|failed to remove root filesystem|Removal In Progress|is already in use by container|removal of container .* is already in progress'

host_prereqs_missing() {   # 0 = something missing (prints what), 1 = all present
  local missing=0 prop droot mdm="${STOIC_MAY_DETACH_MOUNTS:-/proc/sys/fs/may_detach_mounts}"
  if [ -f "${mdm}" ] && [ "$(cat "${mdm}" 2>/dev/null)" != 1 ]; then
    echo "   fs.may_detach_mounts=$(cat "${mdm}" 2>/dev/null) (want 1)"; missing=1
  fi
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    droot=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)
    prop=$(findmnt -no PROPAGATION "${droot}" 2>/dev/null || echo "?")
    case "${prop}" in *slave*) ;; *) echo "   docker root ${droot} propagation '${prop}' (want slave)"; missing=1 ;; esac
  fi
  [ "${missing}" = 1 ]
}

preflight_host() {
  echo "-- preflight: host prerequisites (fs.may_detach_mounts · docker root propagation)"
  local report
  if ! report=$(host_prereqs_missing); then echo "   host prerequisites present"; return 0; fi
  printf '%s\n' "${report}"
  if [ "${PREFLIGHT_NO_HOST_CHANGES}" = 1 ]; then
    echo "!! host prerequisites missing and --no-host-changes given — apply them first:"
    echo "   sudo bash deploy/host-prereqs.sh --yes"
    return 1
  fi
  [ "$(id -u)" = 0 ] || { echo "!! host prerequisites missing and not running as root — run: sudo bash deploy/host-prereqs.sh --yes"; return 1; }
  echo "-- preflight: applying host prerequisites (deploy/host-prereqs.sh --yes) — this is logged below"
  bash deploy/host-prereqs.sh --yes || { echo "!! host prerequisites could not be applied — see FAIL lines above"; return 1; }
  repair_journal host_prereqs "applied by preflight: ${report//$'\n'/; }" 2>/dev/null || true
  return 0
}

# Containers of THIS compose project that compose can no longer manage. Prints "<id> <status> <name>".
leftover_containers() {
  local project c status name
  project=$(basename "$PWD")
  for c in $(docker ps -aq --no-trunc --filter "label=com.docker.compose.project=${project}" 2>/dev/null); do
    status=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true)
    name=$(docker inspect -f '{{.Name}}' "$c" 2>/dev/null | sed 's#^/##' || true)
    case "${status}" in
      dead|removing|created) echo "$c ${status} ${name}" ;;
      *) case "${name}" in [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]_"${project}"-*) echo "$c ${status} ${name}" ;; esac ;;
    esac
  done
}

clean_leftovers() {   # detach orphan copies, then rm the leftovers — containers only, never `-v`, never a volume
  echo "-- preflight: cleaning leftovers of a previous jam"
  if [ "$(id -u)" = 0 ] && docker info >/dev/null 2>&1; then
    STOIC_REPAIR_DOCKER_MOUNTS=1 bash deploy/docker-orphan-mounts.sh --apply 2>/dev/null | grep -E '^-- detached|^== remaining' | sed 's/^/   /' || true
  fi
  local list ids
  list=$(leftover_containers)
  if [ -z "${list// /}" ]; then echo "   no dead/removing/created or renamed leftover containers"; return 0; fi
  printf '%s\n' "${list}" | sed 's/^/   removing /'
  ids=$(printf '%s\n' "${list}" | awk '{print $1}' | tr '\n' ' ')
  repair_journal leftover_rm "ids=${ids}" 2>/dev/null || true
  # shellcheck disable=SC2086
  docker rm -f ${ids} >/dev/null 2>&1 || true
  list=$(leftover_containers)
  [ -z "${list// /}" ] && echo "   leftovers removed" || { echo "!! still present after rm -f:"; printf '%s\n' "${list}" | sed 's/^/   /'; return 1; }
}

reboot_recipe() {
  echo "ERROR: container recreate still fails with overlay EBUSY after one cleanup+retry — reboot required:"
  echo '   docker update --restart=no $(docker ps -aq)'
  echo '   reboot'
  echo '   docker ps -aq | xargs -r docker rm -f'
  echo '   docker compose up -d'
  echo "   (volumes and data are untouched; deploy/doctor.sh → 'docker mount propagation' shows which host processes hold the mounts)"
}

compose_up_guarded() {   # compose_up_guarded [extra compose-up args…]
  local flags="-d --remove-orphans" out log
  [ "$(deploy_mode)" = "registry" ] && flags="${flags} --no-build"
  log=$(mktemp)
  # shellcheck disable=SC2086
  if docker compose up ${flags} "$@" 2>&1 | tee "${log}"; test "${PIPESTATUS[0]}" = 0; then rm -f "${log}"; return 0; fi
  out=$(cat "${log}"); rm -f "${log}"
  if ! printf '%s' "${out}" | grep -qiE "${EBUSY_RE}"; then
    app_boot_failure "$(date -u -d '-10 minutes' +%s 2>/dev/null || echo 0)" || true
    echo "ERROR: docker compose up failed (not an overlay EBUSY problem — see the log above)"
    return 1
  fi
  echo "!! recreate hit overlay EBUSY — cleaning leftovers once more and retrying compose up ONCE"
  clean_leftovers || true
  # shellcheck disable=SC2086
  if docker compose up ${flags} "$@"; then return 0; fi
  reboot_recipe
  return 2
}

# ---------------------------------------------------------------- CI release public key pin
ensure_release_public_key_pin() {   # ensure_release_public_key_pin  (uses PREFLIGHT_YES)
  local cur url kid want sidecar body key fp
  cur=$( { grep -E '^RELEASE_PUBLIC_KEY_B64=' backend/.env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d "\"'")
  want=$( { grep -E '^RELEASE_SIGNER_KEY_ID=' backend/.env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d "\"'"); want="${want:-stoic-release-ed25519-v1}"
  [ -n "${cur}" ] && { echo "   release key: RELEASE_PUBLIC_KEY_B64 pinned (${want})"; return 0; }
  url=$( { grep -E '^RELEASE_SIGNER_PUBLIC_URL=' backend/.env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d "\"'")
  url="${RELEASE_SIGNER_PUBLIC_URL:-${url:-https://stoic-signer.fly.dev}}"
  echo "-- release key: RELEASE_PUBLIC_KEY_B64 is empty — fetching the CI signer's public key from ${url}/public-key"
  if ! body=$(curl -fsS --max-time 8 "${url%/}/public-key" 2>/dev/null); then
    echo "!! release key: ${url} unreachable — CI-signed EA records / model manifests cannot be verified on this host until RELEASE_PUBLIC_KEY_B64 is pinned (docs/RELEASE_SIGNER.md)"; return 0
  fi
  kid=$(printf '%s' "${body}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("key_id",""))' 2>/dev/null || true)
  key=$(printf '%s' "${body}" | python3 -c 'import json,sys; print((json.load(sys.stdin).get("public_key_b64") or "").strip())' 2>/dev/null || true)
  [ "${kid}" = "${want}" ] || { echo "!! release key: signer at ${url} reports key_id '${kid}' but RELEASE_SIGNER_KEY_ID is '${want}' — not pinning"; return 0; }
  fp=$(printf '%s' "${key}" | python3 -c 'import base64,hashlib,sys; k=base64.b64decode(sys.stdin.read().strip()); assert len(k)==32; print("SHA256:"+hashlib.sha256(k).hexdigest()[:32])' 2>/dev/null) \
    || { echo "!! release key: public_key_b64 from ${url} is not a 32-byte Ed25519 key — not pinning"; return 0; }
  sidecar=$(cat secrets/signer_public_key 2>/dev/null || true)
  [ -n "${sidecar}" ] && [ "${key}" = "${sidecar}" ] && { echo "!! release key: ${url} returned the LOCAL runtime sidecar key — refusing to pin it as the CI release key (N102-5)"; return 0; }
  echo "   key_id      ${kid}"
  echo "   public key  ${key}"
  echo "   fingerprint ${fp}"
  if [ "${PREFLIGHT_YES}" != 1 ]; then
    if [ -t 0 ]; then read -r -p "   pin this key as RELEASE_PUBLIC_KEY_B64 in backend/.env? [y/N] " ans; [ "${ans}" = y ] || [ "${ans}" = Y ] || { echo "   not pinned (operator declined)"; return 0; }
    else echo "   not pinned — no terminal to confirm; re-run with --yes to accept the key shown above"; return 0; fi
  fi
  set_kv backend/.env RELEASE_PUBLIC_KEY_B64 "${key}"
  grep -q '^RELEASE_SIGNER_PUBLIC_URL=.' backend/.env 2>/dev/null || set_kv backend/.env RELEASE_SIGNER_PUBLIC_URL "${url}"
  echo "   pinned RELEASE_PUBLIC_KEY_B64 (${kid}, ${fp}) in backend/.env"
  repair_journal release_key_pinned "${kid} ${fp} from ${url}" 2>/dev/null || true
}

# ---------------------------------------------------------------- shared web host detection
shared_web_host_markers() {   # prints the markers found (empty = dedicated host)
  local m=""
  [ -d /usr/local/cpanel ] || [ -x /scripts/rebuildhttpdconf ] && m="${m} cPanel/WHM"
  [ -d /usr/local/psa ] || [ -d /opt/psa ] && m="${m} Plesk"
  [ -d /usr/local/directadmin ] && m="${m} DirectAdmin"
  local svc; for svc in httpd apache2 exim dovecot; do
    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "${svc}" 2>/dev/null; then m="${m} ${svc}"; fi
  done
  printf '%s' "${m# }"
}

record_host_profile() {   # backend/.env STOIC_HOST_PROFILE=shared-web-host|dedicated (+ markers)
  local markers profile
  markers=$(shared_web_host_markers)
  profile=dedicated; [ -n "${markers}" ] && profile=shared-web-host
  [ -f backend/.env ] || return 0
  set_kv backend/.env STOIC_HOST_PROFILE "${profile}"
  set_kv backend/.env STOIC_HOST_MARKERS "\"${markers}\""
  if [ -n "${markers}" ]; then
    echo "!! host: STOIC shares this host with a public web/mail stack (${markers}) — fine for demo-only; migrate to a dedicated host before live trading (docs/HOST_MIGRATION.md)"
  else
    echo "   host: dedicated (no cPanel/Plesk/DirectAdmin/httpd/exim/dovecot detected)"
  fi
}
