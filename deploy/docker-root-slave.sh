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
# Installed by deploy/bootstrap.sh as ExecStartPre of docker.service; safe to re-run
# (no-op when already slave). Run it with dockerd STOPPED when the bind must be redone.
set -u
ROOT="${1:-/var/lib/docker}"
case "$(findmnt -no PROPAGATION "${ROOT}" 2>/dev/null)" in
  *slave*) exit 0 ;;
esac
if mountpoint -q "${ROOT}"; then
  umount -l "${ROOT}" 2>/dev/null || true       # detaches the stale self-bind (and any zombie overlays under it)
fi
if mountpoint -q "${ROOT}"; then
  echo "docker-root-slave: ${ROOT} is a real mount point (separate filesystem) — demoting in place"
else
  mkdir -p "${ROOT}"
  mount --bind "${ROOT}" "${ROOT}" || exit 1
fi
mount --make-slave "${ROOT}" || exit 1
echo "docker-root-slave: ${ROOT} propagation is now $(findmnt -no PROPAGATION "${ROOT}")"
