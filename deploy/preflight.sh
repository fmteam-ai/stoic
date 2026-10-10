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
    # M117-7 — overlay2 must be UNBINDABLE: a cPanel VirtFS `rbind /var/lib` copies live overlay mounts (→ rm EBUSY)
    # M120-1 — only where VirtFS exists (cPanel); dedicated hosts keep Docker's default overlay2 propagation
    odir="${STOIC_OVERLAY_DIR:-${droot}/overlay2}"
    if [ -d "${odir}" ] && virtfs_host; then
      oprop=$(findmnt -no PROPAGATION "${odir}" 2>/dev/null || echo "?")
      case "${oprop}" in *unbindable*) ;; *) echo "   docker overlay2 ${odir} propagation '${oprop}' (want unbindable — VirtFS rbind copies)"; missing=1 ;; esac
    fi
  fi
  # M117-1 — the signed host-profile refresh timer is a prerequisite too: without it the profile expires 24 h after
  # every update (live: blocks trading daily). Checked on EVERY update so hosts that already have (a)+(b) get it.
  if [ "${STOIC_SKIP_HOST_TIMER:-0}" != 1 ] && command -v systemctl >/dev/null 2>&1; then
    if ! systemctl is-active --quiet stoic-host-profile.timer 2>/dev/null; then
      echo "   stoic-host-profile.timer not active (want installed + enabled; refreshes the signed host profile every 6 h)"; missing=1
    fi
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
  # M114-4 — the dockerd restart (~20 s, every container) happens without a prompt ONLY under --yes;
  # otherwise host-prereqs.sh asks on a terminal, or refuses with the exact command
  local yes=(); [ "${PREFLIGHT_YES}" = 1 ] && yes=(--yes)
  echo "-- preflight: applying host prerequisites (deploy/host-prereqs.sh ${yes[*]:-}) — this is logged below"
  bash deploy/host-prereqs.sh "${yes[@]}" || { echo "!! host prerequisites could not be applied — see FAIL lines above (non-interactive: deploy/update.sh --yes)"; return 1; }
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
  if [ -n "${list// /}" ]; then
    # M117-5 — "unlinkat …/merged: device or resource busy": the dead container's overlay is STILL MOUNTED in the
    # host namespace because dockerd's umount hit EBUSY (cPanel scanners/php-fpm/dovecot holding files inside the
    # rootfs). Nothing runs in a dead container ⇒ lazy-unmount its merged dir, show the holders, retry rm once.
    printf '%s\n' "${list}" | awk '{print $1}' | while read -r id; do
      [ -n "${id}" ] || continue
      err=$(docker rm -f "${id}" 2>&1 >/dev/null | tail -1 || true)
      [ -n "${err}" ] && echo "   ${id:0:12}: ${err#Error response from daemon: }"
      m=$(docker inspect --format '{{.GraphDriver.Data.MergedDir}}' "${id}" 2>/dev/null || true)
      if [ -n "${m}" ] && [ "$(id -u)" = 0 ] && mountpoint -q "${m}" 2>/dev/null; then
        command -v fuser >/dev/null 2>&1 && { fuser -vm "${m}" 2>&1 | awk 'NR>1 && NR<=7 {print "      holder: "$0}' || true; }
        repair_journal leftover_umount "id=${id} merged=${m}" 2>/dev/null || true
        umount -l "${m}" 2>/dev/null && echo "   ${id:0:12}: lazily unmounted ${m}"
        docker rm -f "${id}" >/dev/null 2>&1 && echo "   ${id:0:12}: removed after unmount"
      fi
    done
    list=$(leftover_containers)
  fi
  [ -z "${list// /}" ] && echo "   leftovers removed" || { echo "!! still present after rm -f (exclude /var/lib/docker from imunify360/clamd/lfd/maldet, or restart the holder service shown above, then re-run):"; printf '%s\n' "${list}" | sed 's/^/   /'; return 1; }
}

reboot_recipe() {
  echo "ERROR: container recreate still fails with overlay EBUSY after one cleanup+retry — reboot required:"
  echo '   docker update --restart=no $(docker ps -aq)'
  echo '   reboot'
  echo "   # after the reboot (images are already built — the update RESUMES at the restart step, M119-3):"
  echo "   cd $(pwd) && sudo bash deploy/update.sh ${STOIC_UPDATE_REF:-<ref>}"
  echo "   (volumes and data are untouched; deploy/doctor.sh → 'docker mount propagation' shows which host processes hold the mounts;"
  echo "    on cPanel hosts the permanent fix is deploy/move-docker-root.sh /srv/docker — M119-2)"
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
  log=$(mktemp)
  # shellcheck disable=SC2086
  if docker compose up ${flags} "$@" 2>&1 | tee "${log}"; test "${PIPESTATUS[0]}" = 0; then rm -f "${log}"; return 0; fi
  out=$(cat "${log}"); rm -f "${log}"
  # M114-6 — only a SECOND overlay EBUSY is a jam; anything else (bad image, boot failure) is an
  # ordinary failure and takes the caller's normal rollback path
  if ! printf '%s' "${out}" | grep -qiE "${EBUSY_RE}"; then
    app_boot_failure "$(date -u -d '-10 minutes' +%s 2>/dev/null || echo 0)" || true
    echo "ERROR: docker compose up failed on the retry for a reason other than overlay EBUSY (see the log above)"
    return 1
  fi
  reboot_recipe
  return 2
}

# M114-2 — a jammed recreate must never leave worker-trading running without protection/reconciliation:
# stop it, record TRADING PAUSED (platform_state.deploy_jam → readiness), journal it.
pause_trading_after_jam() {   # pause_trading_after_jam <ref>
  echo "-- jam: stopping worker-trading so no new exposure runs without the protection/reconciliation workers"
  docker compose stop -t 30 worker-trading 2>&1 | sed 's/^/   /' || true
  repair_journal trading_paused "overlay EBUSY jam during $1 — worker-trading stopped" 2>/dev/null || true
  if docker compose exec -T backend python ops/deploy_jam.py set --reason "deploy ${1} jammed on overlay EBUSY — worker-trading stopped pending reboot" >/dev/null 2>&1; then
    echo "   readiness now shows TRADING PAUSED (platform_state.deploy_jam) until a clean deploy/update.sh or deploy/restart.sh --env-changed"
  else
    echo "!! could not record the TRADING PAUSED marker (backend not reachable) — readiness will show the missing worker-trading lease instead"
  fi
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD 2>/dev/null || echo '?') trading-paused jam=$1" >> deploy/releases.log
}
clear_deploy_jam_marker() {   # after a CLEAN recreate
  docker compose exec -T backend python ops/deploy_jam.py clear 2>/dev/null | grep -q '"cleared": 1' \
    && { echo "   deploy-jam marker cleared (TRADING PAUSED lifted — worker-trading is managed by compose again)"; repair_journal trading_resumed "clean recreate" 2>/dev/null || true; }
  return 0
}

# ---------------------------------------------------------------- M119-2 — cPanel VirtFS vs docker root under /var/lib
# VirtFS rbinds /var/lib into every jailshell; the bind copies of live container rootfs mounts make `docker rm`
# fail with EBUSY on every recreate (the recurring deploy jam). The only clean fix is a docker root OUTSIDE
# /var/lib — deploy/move-docker-root.sh. The update REFUSES until it is done (PREFLIGHT_YES=1 runs the move).
virtfs_docker_root_gate() {
  local vdir="${STOIC_VIRTFS_DIR:-/home/virtfs}" droot target="${STOIC_DOCKER_ROOT_TARGET:-/srv/docker}" jailed=0
  command -v docker >/dev/null 2>&1 || return 0
  [ -d "${vdir}" ] && [ -n "$(ls -A "${vdir}" 2>/dev/null)" ] && jailed=1
  [ "${jailed}" = 1 ] || grep -qs 'jailshell' /etc/passwd && jailed=1
  [ "${jailed}" = 1 ] || return 0
  droot=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker); droot="${droot:-/var/lib/docker}"
  case "${droot}" in /var/lib/*) ;; *) echo "   cPanel VirtFS present — docker root ${droot} is outside /var/lib (ok)"; return 0 ;; esac
  echo "!! cPanel VirtFS (${vdir} / jailshell users) rbinds /var/lib into every jail — with the docker root at ${droot} every"
  echo "   container recreate can jam on EBUSY. The docker root must move out of /var/lib before this update continues."
  if [ "${PREFLIGHT_YES}" = 1 ] && [ "${PREFLIGHT_NO_HOST_CHANGES}" != 1 ]; then
    echo "-- preflight: moving the docker root to ${target} (deploy/move-docker-root.sh — every container stops for the move)"
    bash deploy/move-docker-root.sh "${target}" --yes || return 1
    return 0
  fi
  echo "   fix (one-off, ~1 min, containers stop during the move):  sudo bash deploy/move-docker-root.sh ${target} --yes"
  echo "   then re-run: sudo bash deploy/update.sh ${STOIC_UPDATE_REF:-<ref>}   (or PREFLIGHT_YES=1 to let update.sh do the move)"
  return 1
}

# ---------------------------------------------------------------- CI release public key pin
key_fingerprint() {   # key_fingerprint <b64>  → SHA256:<64 hex>  (fails on anything but a 32-byte key)
  printf '%s' "$1" | python3 -c 'import base64,hashlib,sys; k=base64.b64decode(sys.stdin.read().strip()); assert len(k)==32; print("SHA256:"+hashlib.sha256(k).hexdigest())' 2>/dev/null
}
RELEASE_KEY_FINGERPRINT_FILE="${RELEASE_KEY_FINGERPRINT_FILE:-release/release_key.fingerprint}"
expected_release_fingerprint() {   # expected_release_fingerprint <key_id> → committed SHA256:… for that key id (empty = none committed)
  awk -v k="$1" '$1 == k {print $2}' "${RELEASE_KEY_FINGERPRINT_FILE}" 2>/dev/null | head -1
}
release_key_status() {   # release_key_status <key_id> → current|transition|revoked (S-1; empty = no status column / unknown id)
  awk -v k="$1" '$1 == k {print $3}' "${RELEASE_KEY_FINGERPRINT_FILE}" 2>/dev/null | head -1
}
current_release_key_id() {   # the key id the repo marks `current` (empty when the file has no status column)
  awk '$1 !~ /^#/ && $3 == "current" {print $1}' "${RELEASE_KEY_FINGERPRINT_FILE}" 2>/dev/null | head -1
}

ensure_release_public_key_pin() {   # ensure_release_public_key_pin  (uses PREFLIGHT_YES)
  local cur url kid want sidecar body key fp exp st repo_cur
  cur=$( { grep -E '^RELEASE_PUBLIC_KEY_B64=' backend/.env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d "\"'")
  want=$( { grep -E '^RELEASE_SIGNER_KEY_ID=' backend/.env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d "\"'"); want="${want:-stoic-release-ed25519-v1}"
  url=$( { grep -E '^RELEASE_SIGNER_PUBLIC_URL=' backend/.env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d "\"'")
  url="${RELEASE_SIGNER_PUBLIC_URL:-${url:-https://stoic-signer.fly.dev}}"
  exp=$(expected_release_fingerprint "${want}")
  st=$(release_key_status "${want}"); repo_cur=$(current_release_key_id)
  # S-1 — the repo says which key id is current: a host still on a revoked/transition id must re-pin (docs/RELEASE_KEY_ROTATION.md)
  case "${st}" in
    revoked) echo "!! release key: RELEASE_SIGNER_KEY_ID=${want} is REVOKED in ${RELEASE_KEY_FINGERPRINT_FILE} — re-pin to ${repo_cur:-the current key}: sudo bash deploy/rotate-release-pin.sh ${repo_cur:-<key_id>}" ;;
    transition) echo "!! release key: RELEASE_SIGNER_KEY_ID=${want} is a TRANSITION key — the current CI key is ${repo_cur}; re-pin: sudo bash deploy/rotate-release-pin.sh ${repo_cur}" ;;
    *) [ -n "${repo_cur}" ] && [ "${repo_cur}" != "${want}" ] && echo "!! release key: repo marks ${repo_cur} current but RELEASE_SIGNER_KEY_ID=${want} — re-pin: sudo bash deploy/rotate-release-pin.sh ${repo_cur}" ;;
  esac
  if [ -n "${cur}" ]; then
    # M114-3 — an existing pin is checked against the committed fingerprint and the live signer; drift WARNS (never silently rewritten)
    fp=$(key_fingerprint "${cur}" || echo "invalid")
    if [ -n "${exp}" ] && [ "${fp}" != "${exp}" ]; then echo "!! release key: pinned RELEASE_PUBLIC_KEY_B64 (${fp}) differs from the committed CI key fingerprint ${exp} (${RELEASE_KEY_FINGERPRINT_FILE}) — stale or wrong pin; re-pin: remove the line from backend/.env and re-run with --yes"
    else echo "   release key: RELEASE_PUBLIC_KEY_B64 pinned (${want}, ${fp})$( [ -n "${exp}" ] && echo ' — matches the committed fingerprint')"; fi
    if body=$(curl -fsS --max-time 8 "${url%/}/public-key" 2>/dev/null); then
      key=$(printf '%s' "${body}" | python3 -c 'import json,sys; print((json.load(sys.stdin).get("public_key_b64") or "").strip())' 2>/dev/null || true)
      [ -n "${key}" ] && [ "${key}" != "${cur}" ] && echo "!! release key: the signer at ${url} serves a DIFFERENT key ($(key_fingerprint "${key}" || echo invalid)) than the pinned one — verify before the next release (docs/RELEASE_SIGNER.md)"
    fi
    return 0
  fi
  echo "-- release key: RELEASE_PUBLIC_KEY_B64 is empty — fetching the CI signer's public key from ${url}/public-key"
  if ! body=$(curl -fsS --max-time 8 "${url%/}/public-key" 2>/dev/null); then
    echo "!! release key: ${url} unreachable — CI-signed EA records / model manifests cannot be verified on this host until RELEASE_PUBLIC_KEY_B64 is pinned (docs/RELEASE_SIGNER.md)"; return 0
  fi
  kid=$(printf '%s' "${body}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("key_id",""))' 2>/dev/null || true)
  key=$(printf '%s' "${body}" | python3 -c 'import json,sys; print((json.load(sys.stdin).get("public_key_b64") or "").strip())' 2>/dev/null || true)
  [ "${kid}" = "${want}" ] || { echo "!! release key: signer at ${url} reports key_id '${kid}' but RELEASE_SIGNER_KEY_ID is '${want}' — not pinning"; return 0; }
  fp=$(key_fingerprint "${key}") || { echo "!! release key: public_key_b64 from ${url} is not a 32-byte Ed25519 key — not pinning"; return 0; }
  # M114-3 — first fetch trusts the REPO, not the network: the fetched key must match the committed fingerprint
  if [ -n "${exp}" ] && [ "${fp}" != "${exp}" ]; then
    echo "!! release key: ${url} returned ${fp} but the committed CI key fingerprint is ${exp} (${RELEASE_KEY_FINGERPRINT_FILE}) — NOT pinning (possible MITM or key rotation without a repo update)"; return 0
  fi
  # audit #15 P3 — no trust-on-first-use: every pinnable key id must be fingerprinted in the repo
  [ -n "${exp}" ] || { echo "!! release key: no committed fingerprint for ${want} in ${RELEASE_KEY_FINGERPRINT_FILE} — NOT pinning (add the fingerprint to the repo via a signed release first)"; return 0; }
  [ "${st}" = revoked ] && { echo "!! release key: ${want} is REVOKED in ${RELEASE_KEY_FINGERPRINT_FILE} — NOT pinning"; return 0; }
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
  echo "   pinned RELEASE_PUBLIC_KEY_B64 (${kid}, ${fp}$( [ -n "${exp}" ] && echo ', matches the committed fingerprint')) in backend/.env"
  repair_journal release_key_pinned "${kid} ${fp} from ${url}" 2>/dev/null || true
}

# ---------------------------------------------------------------- shared web host detection
virtfs_host() {   # M120-1 — cPanel VirtFS present (jailshell rbind copies of /var/lib) · STOIC_WANT_OVERLAY_UNBINDABLE=1 forces
  [ "${STOIC_WANT_OVERLAY_UNBINDABLE:-}" = 1 ] || [ -d /home/virtfs ] || [ -d /usr/local/cpanel ]
}

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

host_profile_sig() {   # host_profile_sig <key> <profile> <markers> <detected_at>  (derived key, length-prefixed canonical payload)
  python3 -c 'import hmac, hashlib, sys; dk = hmac.new(sys.argv[1].encode(), b"stoic-host-profile-v1", hashlib.sha256).digest(); payload = "".join("%d:%s;" % (len(v), v) for v in sys.argv[2:5]).encode(); print(hmac.new(dk, payload, hashlib.sha256).hexdigest())' "$@" 2>/dev/null
}
write_host_profile_file() {   # write_host_profile_file <profile> <markers> <detected_at> <sig>  → deploy/state/host_profile.json (A19-P1-04)
  # A20-P1-04 — every step is checked: a failed mkdir/write/mv/chmod returns non-zero with the path and error, the
  # previous file stays intact and the temp file is removed (host-profile-refresh.sh exits 1 ⇒ systemd unit FAILED).
  local dir="${STOIC_HOST_PROFILE_DIR:-deploy/state}" tmp err
  tmp="${dir}/host_profile.json.tmp"
  if ! err=$(mkdir -p "${dir}" 2>&1); then echo "!! host profile: cannot create ${dir}: ${err}" >&2; return 1; fi
  if ! err=$(python3 -c 'import json, sys; json.dump({"profile": sys.argv[1], "markers": sys.argv[2], "detected_at": sys.argv[3], "sig": sys.argv[4], "schema": 1}, open(sys.argv[5], "w"), sort_keys=True)' "$1" "$2" "$3" "$4" "${tmp}" 2>&1); then
    echo "!! host profile: cannot write ${tmp}: ${err##*$'\n'}" >&2; rm -f "${tmp}" 2>/dev/null; return 1
  fi
  if ! err=$(chmod 0644 "${tmp}" 2>&1); then echo "!! host profile: chmod ${tmp} failed: ${err}" >&2; rm -f "${tmp}" 2>/dev/null; return 1; fi
  if ! err=$(mv -f "${tmp}" "${dir}/host_profile.json" 2>&1); then echo "!! host profile: cannot replace ${dir}/host_profile.json: ${err}" >&2; rm -f "${tmp}" 2>/dev/null; return 1; fi
  return 0
}

write_key_ages_file() {   # M120-2 — deploy/state/key_ages.json: release key created=, runtime key mtime, Origin CA notAfter (advisory, unsigned)
  local dir="${STOIC_HOST_PROFILE_DIR:-deploy/state}" kid cert_end=""
  kid=$( { grep -E '^RELEASE_SIGNER_KEY_ID=' backend/.env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d "\"'")
  kid="${kid:-$(current_release_key_id)}"; kid="${kid:-stoic-release-ed25519-v1}"
  [ -f secrets/origin_cert.pem ] && cert_end=$(openssl x509 -enddate -noout -in secrets/origin_cert.pem 2>/dev/null | cut -d= -f2-)
  mkdir -p "${dir}" 2>/dev/null || { echo "!! key ages: cannot create ${dir}" >&2; return 1; }
  python3 - "${RELEASE_KEY_FINGERPRINT_FILE}" "${kid}" secrets/signer_ed25519_key secrets/origin_cert.pem "${cert_end}" "${dir}/key_ages.json" <<'PY' || { echo "!! key ages: ${dir}/key_ages.json not written" >&2; return 1; }
import json, os, sys
from datetime import datetime
fp_file, kid, rk, cert, cert_end, out = sys.argv[1:7]
created = None
try:
    for line in open(fp_file):
        parts = line.split()
        if parts and parts[0] == kid:
            created = next((p.split("=", 1)[1] for p in parts[2:] if p.startswith("created=")), None)
except OSError:
    pass
iso = lambda ts: datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")
not_after = None
if cert_end.strip():
    try:
        not_after = datetime.strptime(" ".join(cert_end.split()), "%b %d %H:%M:%S %Y %Z").strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        not_after = None
doc = {"schema": 1, "generated_at": iso(datetime.utcnow().timestamp()),
       "release_key": {"key_id": kid, "created": created},
       "runtime_key": {"path": rk, "created": iso(os.path.getmtime(rk)) if os.path.exists(rk) else None},
       "origin_cert": {"path": cert, "present": os.path.exists(cert), "not_after": not_after}}
tmp = out + ".tmp"
json.dump(doc, open(tmp, "w"), sort_keys=True)
os.chmod(tmp, 0o644)
os.replace(tmp, out)
PY
}

record_host_profile() {   # backend/.env STOIC_HOST_PROFILE=shared-web-host|dedicated + markers + detected_at + HMAC (M114-7)
  local markers profile at key sig
  markers=$(shared_web_host_markers)
  profile=dedicated; [ -n "${markers}" ] && profile=shared-web-host
  [ -f backend/.env ] || return 0
  at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  set_kv backend/.env STOIC_HOST_PROFILE "${profile}"
  set_kv backend/.env STOIC_HOST_MARKERS "\"${markers}\""
  set_kv backend/.env STOIC_HOST_DETECTED_AT "${at}"
  # M114-7/M115-3 — signed with a key DERIVED (HKDF-style, label stoic-host-profile-v1) from the ledger anchor
  # key (secrets/ledger_anchor_key → LEDGER_ANCHOR_KEY in the backend); a hand-edited `dedicated` without the
  # matching signature is reported UNVERIFIED (blocks in production). Limit: root can re-sign — this stops
  # accidental edits/drift, not a determined admin. M115-1: values go in as argv (no f-string escapes —
  # python 3.6 safe) and a signing failure is NON-fatal: the profile is recorded unsigned with a warning.
  key=$(cat secrets/ledger_anchor_key 2>/dev/null || { grep -E '^LEDGER_ANCHOR_KEY=' backend/.env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d "\"'")
  sig=""
  if [ -n "${key}" ]; then sig=$(host_profile_sig "${key}" "${profile}" "${markers}" "${at}") || sig=""; fi
  write_host_profile_file "${profile}" "${markers}" "${at}" "${sig}" || echo "!! host: deploy/state/host_profile.json not written (see error above) — readiness reports the profile unverified until deploy/host-profile-refresh.sh succeeds"   # A19-P1-04 — the file the containers read (ro mount)
  write_key_ages_file || true   # M120-2 — advisory key/cert ages (readiness warns at 180 d / 30 d)
  if [ -n "${sig}" ]; then set_kv backend/.env STOIC_HOST_PROFILE_SIG "${sig}"
  else
    set_kv backend/.env STOIC_HOST_PROFILE_SIG ""
    echo "!! host: host profile recorded UNSIGNED ($( [ -n "${key}" ] && echo 'python3 HMAC failed' || echo 'no ledger anchor key available')) — readiness reports it unverified"
  fi
  if [ -n "${markers}" ]; then
    echo "!! host: STOIC shares this host with a public web/mail stack (${markers}) — fine for demo-only; migrate to a dedicated host before live trading (docs/HOST_MIGRATION.md)"
  else
    echo "   host: dedicated (no cPanel/Plesk/DirectAdmin/httpd/exim/dovecot detected)"
  fi
}
