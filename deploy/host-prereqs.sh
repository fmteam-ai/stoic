#!/usr/bin/env bash
# Host prerequisites for a Docker stack on cPanel / RHEL-8 family hosts — idempotent, root only.
# Fresh installs (deploy/bootstrap.sh) and existing installs (deploy/update.sh, deploy/restart.sh)
# share THIS code path, so a host set up before these fixes existed gets them on its next update.
#
#   a. fs.may_detach_mounts=1 (now + /etc/sysctl.d/99-stoic-docker.conf) — without it `docker rm`
#      fails "driver overlay2 failed to remove root filesystem … device or resource busy" whenever a
#      container's overlay mount leaked into another mount namespace (CageFS/LVE, httpd PrivateTmp).
#   b. docker root as a SLAVE mount of `/` (deploy/docker-root-slave.sh as docker.service ExecStartPre)
#      — container rootfs mounts stop propagating into every sandboxed service's namespace. When the
#      propagation is not slave yet, dockerd is restarted ONCE (~20 s API downtime, announced first).
#   c. MountFlags=slave drop-ins for the systemd units that already hold copies (deploy/host-mount-fix.sh
#      --apply; drop-ins only — the cPanel units are restarted only with --restart).
#
#   deploy/host-prereqs.sh            apply (a)(b)(c), print the PASS/FIX/WARN summary
#   deploy/host-prereqs.sh --check    report only, exit 2 when (a) or (b) is missing — used by update.sh
#   deploy/host-prereqs.sh --restart  …and restart the leaking cPanel units after (c)
#   deploy/host-prereqs.sh --yes      no prompt before the dockerd restart (bootstrap/update)
# Exit 1 only when (a) or (b) could not be applied. Re-running changes nothing the second time.
set -u
cd "$(dirname "$0")/.." || exit 1
CHECK=0; RESTART=0; YES=0
for a in "$@"; do
  case "$a" in
    --check) CHECK=1 ;;
    --restart) RESTART=1 ;;
    --yes|-y) YES=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $a"; exit 64 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "host-prereqs: run as root"; exit 1; }

# paths are overridable for the shell tests (stubbed docker/findmnt/sysctl) — production uses the defaults
SYSCTL_CONF="${STOIC_SYSCTL_CONF:-/etc/sysctl.d/99-stoic-docker.conf}"
SLAVE_BIN="${STOIC_SLAVE_BIN:-/usr/local/sbin/stoic-docker-root-slave}"
DROPIN="${STOIC_DOCKER_DROPIN:-/etc/systemd/system/docker.service.d/10-stoic-private-root.conf}"
MDM="${STOIC_MAY_DETACH_MOUNTS:-/proc/sys/fs/may_detach_mounts}"
SUMMARY=(); HARD_FAIL=0; CHANGED=0
note() { SUMMARY+=("$1  $2"); case "$1" in FIX) CHANGED=1 ;; esac; }
log()  { echo "-- host-prereqs: $*"; }

# ---------------------------------------------------------------- (a) fs.may_detach_mounts
if [ -f "${MDM}" ]; then
  cur=$(cat "${MDM}" 2>/dev/null || echo "?")
  persisted=0; grep -qsE '^\s*fs\.may_detach_mounts\s*=\s*1\s*$' "${SYSCTL_CONF}" && persisted=1
  if [ "${cur}" = 1 ] && [ "${persisted}" = 1 ]; then
    note PASS "fs.may_detach_mounts=1 (persisted in ${SYSCTL_CONF})"
  elif [ "${CHECK}" = 1 ]; then
    note FIX "fs.may_detach_mounts=${cur} persisted=${persisted} → would set 1 + write ${SYSCTL_CONF}"; HARD_FAIL=1
  else
    if [ "${persisted}" != 1 ]; then printf 'fs.may_detach_mounts = 1\n' > "${SYSCTL_CONF}" && log "wrote ${SYSCTL_CONF}"; fi
    if sysctl -q -w fs.may_detach_mounts=1 2>/dev/null && [ "$(cat "${MDM}")" = 1 ]; then
      log "fs.may_detach_mounts: ${cur} → 1"; note FIX "fs.may_detach_mounts set to 1 (was ${cur}); persisted in ${SYSCTL_CONF}"
    else
      note FAIL "fs.may_detach_mounts could not be set (sysctl -w failed)"; HARD_FAIL=1
    fi
  fi
else
  note PASS "kernel has no fs.may_detach_mounts knob (not RHEL-8 family) — nothing to do"
fi

# ---------------------------------------------------------------- (b) docker root slave propagation
if ! command -v docker >/dev/null 2>&1 || ! command -v systemctl >/dev/null 2>&1; then
  note WARN "docker or systemd not installed yet — docker root propagation is applied after Docker is installed"
else
  DROOT=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)
  if [ "${CHECK}" = 1 ]; then
    cmp -s deploy/docker-root-slave.sh "${SLAVE_BIN}" 2>/dev/null || { note FIX "${SLAVE_BIN} missing/outdated → would install"; HARD_FAIL=1; }
    [ -f "${DROPIN}" ] || { note FIX "${DROPIN} missing → would install ExecStartPre drop-in"; HARD_FAIL=1; }
  else
    if ! cmp -s deploy/docker-root-slave.sh "${SLAVE_BIN}" 2>/dev/null; then
      install -m 0755 deploy/docker-root-slave.sh "${SLAVE_BIN}" && { log "installed ${SLAVE_BIN}"; note FIX "installed ${SLAVE_BIN}"; }
    fi
    want=$(printf '[Service]\nExecStartPre=-%s %s\n' "${SLAVE_BIN}" "${DROOT}")
    if [ "$(cat "${DROPIN}" 2>/dev/null)" != "${want}" ]; then
      mkdir -p "$(dirname "${DROPIN}")" && printf '%s\n' "${want}" > "${DROPIN}" && systemctl daemon-reload \
        && { log "wrote ${DROPIN}"; note FIX "docker.service drop-in ${DROPIN} written (ExecStartPre)"; }
    fi
  fi
  PROP=$(findmnt -no PROPAGATION "${DROOT}" 2>/dev/null || echo "?")
  case "${PROP}" in
    *slave*) note PASS "${DROOT} propagation is '${PROP}' (slave)" ;;
    *)
      if [ "${CHECK}" = 1 ]; then
        note FIX "${DROOT} propagation is '${PROP}' → would make it slave (one dockerd restart, ~20 s)"; HARD_FAIL=1
      elif ! docker info >/dev/null 2>&1; then
        note WARN "dockerd not running — ${DROOT} will become slave at the next docker start (ExecStartPre)"
      else
        echo "!! ${DROOT} propagation is '${PROP}' — making it a slave mount requires ONE dockerd restart: every container stops for ~20 s (API offline)."
        if [ "${YES}" != 1 ]; then
          if [ -t 0 ]; then read -r -p "   proceed now? [y/N] " ans; [ "${ans}" = y ] || [ "${ans}" = Y ] || { note FAIL "docker root propagation left '${PROP}' (operator declined) — re-run with --yes"; HARD_FAIL=1; }
          else note FAIL "docker root propagation is '${PROP}' and no terminal to confirm — re-run with --yes"; HARD_FAIL=1; fi
        fi
        if [ "${HARD_FAIL}" = 0 ]; then
          log "stopping docker (${PROP} → slave)"
          systemctl stop docker docker.socket 2>/dev/null || true
          "${SLAVE_BIN}" "${DROOT}" || true
          systemctl reset-failed docker docker.socket 2>/dev/null || true
          systemctl start docker
          for _ in $(seq 1 45); do docker info >/dev/null 2>&1 && break; sleep 2; done
          PROP=$(findmnt -no PROPAGATION "${DROOT}" 2>/dev/null || echo "?")
          case "${PROP}" in
            *slave*) log "${DROOT} propagation is now '${PROP}'"; note FIX "${DROOT} made a slave mount (dockerd restarted once)" ;;
            *) note FAIL "${DROOT} propagation is still '${PROP}' after the restart — run: systemctl stop docker && ${SLAVE_BIN} ${DROOT} && systemctl start docker"; HARD_FAIL=1 ;;
          esac
        fi
      fi ;;
  esac
fi

# ---------------------------------------------------------------- (c) MountFlags=slave drop-ins for leaking units
if [ "${CHECK}" = 1 ] || [ "${STOIC_SKIP_MOUNT_FIX:-0}" = 1 ] || ! command -v systemctl >/dev/null 2>&1; then
  note PASS "host-mount-fix: $( [ "${CHECK}" = 1 ] && echo 'not evaluated in --check mode' || { [ "${STOIC_SKIP_MOUNT_FIX:-0}" = 1 ] && echo 'skipped (STOIC_SKIP_MOUNT_FIX=1)' || echo 'no systemd — skipped'; })"
else
  args=(--apply); [ "${RESTART}" = 1 ] && args+=(--restart)
  if out=$(bash deploy/host-mount-fix.sh "${args[@]}" 2>&1); then
    wrote=$(printf '%s\n' "${out}" | grep -c '^   wrote ' || true)
    if [ "${wrote}" -gt 0 ]; then printf '%s\n' "${out}" | grep -E '^   (wrote|restarted) ' | sed 's/^/-- host-prereqs: /'; note FIX "MountFlags=slave drop-ins written for ${wrote} unit(s)$( [ "${RESTART}" = 1 ] && echo ', units restarted' || echo ' (restart them with --restart or at the next maintenance window)')"
    else note PASS "no systemd unit holds docker overlay copies without MountFlags=slave"; fi
  else
    note WARN "host-mount-fix.sh failed (non-fatal): $(printf '%s\n' "${out}" | tail -1)"
  fi
fi

# ---------------------------------------------------------------- summary
echo "== host-prereqs ($( [ "${CHECK}" = 1 ] && echo check || echo apply )) =="
for s in "${SUMMARY[@]}"; do echo "   ${s}"; done
if [ "${CHECK}" = 1 ]; then
  [ "${HARD_FAIL}" = 0 ] && { echo "== host prerequisites present"; exit 0; } || { echo "== host prerequisites MISSING — apply with: sudo bash deploy/host-prereqs.sh --yes"; exit 2; }
fi
[ "${HARD_FAIL}" = 0 ] && { echo "== host prerequisites $( [ "${CHANGED}" = 1 ] && echo applied || echo 'already in place (nothing changed)')"; exit 0; }
echo "== host prerequisites INCOMPLETE (see FAIL lines)"; exit 1
