#!/bin/sh
# Make the Docker root a SLAVE mount of the host root so container rootfs mounts
# never propagate into other mount namespaces (php-fpm pools, mariadb, named …
# on cPanel hosts) — otherwise the RHEL 8 kernel refuses to rmdir a container's
# merged dir while any copy exists and `docker rm` fails "device or resource busy".
#
# Why slave and not private: dockerd flips a PRIVATE root back to shared on start
# (setupDaemonRootPropagation); it accepts shared or slave. Why the umount first:
# `make-slave` on a shared mount that has no peers (the self-bind dockerd creates)
# yields PRIVATE again — the bind must be created from the plain directory (peer of
# `/`, shared:1) and then demoted, giving master:1 = a real slave.
#
# M114-1 — a REAL partition/disk at the docker root (findmnt SOURCE is a plain device, not
# "<dev>[<root>]") is NEVER unmounted: unmounting it would hide every image and volume behind an
# empty bind (Mongo would start empty). It is bound over itself (a peer of the partition mount)
# and the bind is demoted in place. Only Docker's own self-bind "<dev>[<root>]" is detached.
#
# Installed by deploy/host-prereqs.sh as ExecStartPre of docker.service; safe to re-run
# (no-op when already slave). Run it with dockerd STOPPED when the bind must be redone.
set -u
ROOT="${1:-/var/lib/docker}"

# M117-7 — slave propagation stops FUTURE propagation, but a fresh `mount --rbind /var/lib …` (cPanel VirtFS
# jailshell, every jailed login/cron) still COPIES every overlay mount that exists at that moment, and the copy
# makes `docker rm` fail "unlinkat …/merged: device or resource busy". rbind skips UNBINDABLE mounts and their
# children, so the overlay2 tree is made unbindable. Docker never bind-mounts FROM overlay2 (volumes/ and
# containers/ are untouched), and overlay mounts are created as children — unaffected. Applied only while no
# overlay is mounted (dockerd stopped = ExecStartPre): a bind over a dir with live submounts would hide them.
overlay_unbindable() {
  d="${STOIC_OVERLAY_DIR:-${ROOT}/overlay2}"
  [ -d "${d}" ] || return 0
  case "$(findmnt -no PROPAGATION "${d}" 2>/dev/null)" in *unbindable*) return 0 ;; esac
  if findmnt -rn -t overlay -o TARGET 2>/dev/null | grep -q "^${d}/"; then
    echo "docker-root-slave: ${d} not yet unbindable — applied at the next dockerd restart (overlay mounts are live now)"
    return 0
  fi
  mountpoint -q "${d}" || mount --bind "${d}" "${d}" || return 1
  mount --make-unbindable "${d}" || return 1
  echo "docker-root-slave: ${d} is now unbindable (rbind copies by cPanel VirtFS jails skip the container rootfs mounts)"
}

case "$(findmnt -no PROPAGATION "${ROOT}" 2>/dev/null)" in
  *slave*) overlay_unbindable; exit 0 ;;
esac
if mountpoint -q "${ROOT}"; then
  SRC=$(findmnt -no SOURCE "${ROOT}" 2>/dev/null || echo "?")
  case "${SRC}" in
    *"[${ROOT}]"*|*"[/]"*)   # Docker's own self-bind of the directory — detach it (and any zombie overlays under it)
      echo "docker-root-slave: detaching the stale self-bind ${SRC} at ${ROOT}"
      umount -l "${ROOT}" 2>/dev/null || true ;;
    *)                       # a real filesystem (separate disk/partition): NEVER unmount — bind over it and demote
      echo "docker-root-slave: ${ROOT} is a real filesystem (${SRC}) — binding over it and demoting in place, nothing is unmounted"
      mount --bind "${ROOT}" "${ROOT}" || exit 1
      mount --make-slave "${ROOT}" || exit 1
      echo "docker-root-slave: ${ROOT} propagation is now $(findmnt -no PROPAGATION "${ROOT}")"
      overlay_unbindable
      exit 0 ;;
  esac
fi
if mountpoint -q "${ROOT}"; then
  echo "docker-root-slave: ${ROOT} is still a mount point after detaching the self-bind — demoting in place"
else
  mkdir -p "${ROOT}"
  mount --bind "${ROOT}" "${ROOT}" || exit 1
fi
mount --make-slave "${ROOT}" || exit 1
echo "docker-root-slave: ${ROOT} propagation is now $(findmnt -no PROPAGATION "${ROOT}")"
overlay_unbindable
