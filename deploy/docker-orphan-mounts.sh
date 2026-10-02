#!/bin/bash
# Docker orphan mounts — cPanel VirtFS (jailed shells) bind-mounts /var/lib into every
# /home/virtfs/<user>/ jail, which copies each container rootfs mount into the HOST mount
# namespace. The copy shares the merged dir's dentry, so once the container is gone
# `docker rm` fails with "unlinkat …/merged: device or resource busy" (kernel
# is_local_mountpoint) — every stack restart retries until the reaper wins.
#
# Scope: Docker only. Detaches (umount -l) host-namespace COPIES of overlay layers that no
# container (running, stopped or created) owns. Never touches the docker root itself, never a
# shared peer, never anything when dockerd is down. No cPanel service, site or jail is affected
# (a jail never needs /var/lib/docker).
#
#   deploy/docker-orphan-mounts.sh           # list what would be detached (dry run)
#   deploy/docker-orphan-mounts.sh --apply   # detach, then re-count
set -u
cd "$(dirname "$0")/.."
. deploy/lib.sh
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
docker info >/dev/null 2>&1 || { echo "dockerd not reachable — refusing (every layer would look orphaned)"; exit 1; }
APPLY=0; for a in "$@"; do case "$a" in --apply) APPLY=1 ;; esac; done

LIST=$(orphan_merged_copies)
TOTAL=$(printf '%s\n' "${LIST}" | grep -c . || true)
echo "== host-namespace merged mounts: $(grep -c '/var/lib/docker/overlay2/[^ ]*/merged' /proc/1/mountinfo) · containers (any state): $(docker ps -aq | wc -l)"
echo "== detachable orphan copies (layer owned by no container, not a shared peer, not under the docker root): ${TOTAL}"
printf '%s\n' "${LIST}" | awk 'NF==2 { split($2, a, "/"); tree = (a[2] == "home" ? "/" a[2] "/" a[3] "/" a[4] : "/" a[2]); c[tree]++; l[$1]=1 }
  END { for (t in c) printf "   %-40s ×%d\n", t, c[t]; n = 0; for (k in l) n++; printf "   distinct dead layers: %d\n", n }' | sort
[ "${TOTAL}" = 0 ] && { echo "== nothing to detach"; exit 0; }
if [ "${APPLY}" != 1 ]; then echo "== dry run — re-run with --apply (umount -l of the paths above only)"; exit 0; fi

export STOIC_REPAIR_DOCKER_MOUNTS=1
detach_orphan_copies
echo "== remaining detachable orphan copies: $(orphan_merged_copies | grep -c . || true)"
