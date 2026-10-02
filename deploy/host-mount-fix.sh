#!/bin/bash
# Host mount fix — stop foreign mount namespaces (cPanel php-fpm pools, httpd,
# mariadb, node apps … started with PrivateTmp=yes) from receiving copies of Docker's
# overlay mounts. A copy held in another namespace makes `docker rm` fail
# "device or resource busy" on every stack restart (the retries in update.sh).
#
# Two complementary layers:
#   1. docker root as SLAVE of `/`      (deploy/docker-root-slave.sh — docker.service ExecStartPre)
#   2. MountFlags=slave on the units that leak  (THIS script — systemd drop-in per unit)
# With (2) a unit's private namespace still sees host mounts, but mounts it creates
# never propagate back — and it stops holding docker's rootfs copies.
#
#   deploy/host-mount-fix.sh              # detect leaking units, print the plan (dry run)
#   deploy/host-mount-fix.sh --apply      # write drop-ins + daemon-reload (no restarts)
#   deploy/host-mount-fix.sh --apply --restart   # …and restart the fixed units (releases current copies)
#   deploy/host-mount-fix.sh --units ea-php81-php-fpm.service,httpd.service --apply
set -u
APPLY=0; RESTART=0; UNITS=""
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --restart) RESTART=1 ;;
    --units) ;;
    --units=*) UNITS="${a#--units=}" ;;
    *) [ -z "${UNITS}" ] && [ "${prev:-}" = "--units" ] && UNITS="$a" ;;
  esac
  prev="$a"
done
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
command -v systemctl >/dev/null || { echo "no systemd on this host — nothing to do"; exit 0; }

DROOT=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)
HOST_NS=$(readlink /proc/1/ns/mnt)

# pid → systemd unit (from the cgroup path); empty when not a systemd service (LVE/CageFS user process)
unit_of() { awk -F/ '{for(i=NF;i>0;i--) if ($i ~ /\.service$/) {print $i; exit}}' "/proc/$1/cgroup" 2>/dev/null; }

detect() {
  for p in /proc/[0-9]*; do
    pid=${p#/proc/}
    ns=$(readlink "$p/ns/mnt" 2>/dev/null) || continue; [ "${ns}" = "${HOST_NS}" ] && continue
    grep -qs "${DROOT}/overlay2/" "$p/mountinfo" || continue
    grep -qs 'docker\|containerd' "$p/cgroup" && continue
    u=$(unit_of "${pid}")
    printf '%s\t%s\t%s\n' "${u:-"-"}" "${pid}" "$(cat "$p/comm" 2>/dev/null)"
  done
}

echo "== docker root propagation: $(findmnt -no PROPAGATION "${DROOT}" 2>/dev/null || echo '?') (want: slave; fix: systemctl stop docker && /usr/local/sbin/stoic-docker-root-slave ${DROOT} && systemctl start docker)"
LEAKS=$(detect)
if [ -z "${UNITS}" ]; then
  UNITS=$(printf '%s\n' "${LEAKS}" | awk -F'\t' '$1 != "-" {print $1}' | sort -u | paste -sd, -)
fi
NOUNIT=$(printf '%s\n' "${LEAKS}" | awk -F'\t' '$1 == "-" {print $3}' | sort | uniq -c | sort -rn | awk '{printf "%s×%s ", $2, $1}')

echo "== processes holding docker overlay copies in a foreign mount namespace: $(printf '%s\n' "${LEAKS}" | grep -c . || true)"
printf '%s\n' "${LEAKS}" | awk -F'\t' 'NF==3 {c[$1" ("$3")"]++} END {for (k in c) printf "   %-50s ×%d\n", k, c[k]}' | sort
[ -n "${NOUNIT// /}" ] && echo "!! not systemd services (LVE/CageFS or manual processes): ${NOUNIT}— restart them after the fix, or run them under a unit with MountFlags=slave"

[ -z "${UNITS}" ] && { echo "== no leaking systemd units detected"; exit 0; }
echo "== units to fix: ${UNITS}"
IFS=, read -r -a ARR <<< "${UNITS}"
for u in "${ARR[@]}"; do
  u=$(echo "$u" | xargs); [ -n "$u" ] || continue
  systemctl cat "$u" >/dev/null 2>&1 || { echo "   skip ${u}: unknown unit"; continue; }
  d="/etc/systemd/system/${u}.d"; f="${d}/10-stoic-mountflags.conf"
  if [ "${APPLY}" = 1 ]; then
    mkdir -p "$d"; printf '[Service]\n# stoic: never hold copies of docker overlay mounts (docker rm EBUSY)\nMountFlags=slave\n' > "$f"
    echo "   wrote ${f}"
  else
    echo "   would write ${f}  [Service] MountFlags=slave"
  fi
done
if [ "${APPLY}" = 1 ]; then
  systemctl daemon-reload
  if [ "${RESTART}" = 1 ]; then
    for u in "${ARR[@]}"; do u=$(echo "$u" | xargs); [ -n "$u" ] && systemctl try-restart "$u" && echo "   restarted ${u}"; done
    echo "== re-check:"; LEFT=$(detect | grep -c . || true); echo "   remaining foreign copies: ${LEFT}"
  else
    echo "== drop-ins written; units keep their CURRENT namespaces until restarted — run again with --restart (or at the next maintenance window)"
  fi
  echo "== systemd drop-ins survive cPanel/EasyApache updates; verify any time with deploy/doctor.sh ('docker mount propagation')"
else
  echo "== dry run — add --apply (and --restart) to make the change"
fi
