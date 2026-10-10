#!/usr/bin/env bash
# M119-2 — move the Docker data root out of /var/lib (cPanel VirtFS rbinds /var/lib into every jailshell and the
# copies make container removal fail with EBUSY — the recurring deploy jam).
#   sudo bash deploy/move-docker-root.sh /srv/docker [--yes]
# stop docker → same-filesystem rename (instant) or rsync -aHAX → /etc/docker/daemon.json data-root → systemd
# drop-in path → start → verify volumes + images → old root kept as <old>.moved-<ts> for the rollback path.
set -euo pipefail
NEW="${1:-/srv/docker}"; YES=0; [ "${2:-}" = "--yes" ] && YES=1
[ "$(id -u)" = 0 ] || { echo "move-docker-root: run as root"; exit 1; }
command -v docker >/dev/null || { echo "move-docker-root: docker not installed"; exit 1; }
OLD=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || true); OLD="${OLD:-/var/lib/docker}"
case "${NEW}" in /var/lib/*) echo "move-docker-root: ${NEW} is still under /var/lib (VirtFS rbinds it) — choose e.g. /srv/docker"; exit 1 ;; esac
[ "${OLD}" = "${NEW}" ] && { echo "move-docker-root: docker root is already ${NEW}"; exit 0; }
[ -e "${NEW}" ] && [ -n "$(ls -A "${NEW}" 2>/dev/null)" ] && { echo "move-docker-root: ${NEW} exists and is not empty — refusing"; exit 1; }
TS=$(date -u +%Y%m%dT%H%M%SZ)
DAEMON=/etc/docker/daemon.json
DROPIN_DIR="${STOIC_DOCKER_DROPIN_DIR:-/etc/systemd/system/docker.service.d}"
VOL_BEFORE=$(docker volume ls -q 2>/dev/null | sort | tr '\n' ' ')
IMG_BEFORE=$(docker image ls -q 2>/dev/null | sort -u | wc -l)
echo "== move docker root ${OLD} → ${NEW} (volumes: $(echo "${VOL_BEFORE}" | wc -w), images: ${IMG_BEFORE})"
if [ "${YES}" != 1 ]; then
  if [ -t 0 ]; then read -r -p "   every container stops for the move. proceed? [y/N] " a; [ "${a}" = y ] || [ "${a}" = Y ] || exit 1
  else echo "move-docker-root: no terminal to confirm — re-run with --yes"; exit 1; fi
fi
systemctl stop docker docker.socket containerd 2>/dev/null || true
mkdir -p "$(dirname "${NEW}")"
if [ "$(stat -c %d "$(dirname "${NEW}")")" = "$(stat -c %d "${OLD}")" ]; then
  mv "${OLD}" "${NEW}"; echo "   renamed (same filesystem)"; MOVED=rename
else
  command -v rsync >/dev/null || { echo "move-docker-root: rsync required for a cross-filesystem move"; systemctl start docker; exit 1; }
  rsync -aHAX --numeric-ids "${OLD}/" "${NEW}/"; echo "   copied with rsync -aHAX"; MOVED=copy
fi
python3 - "${DAEMON}" "${NEW}" <<'PY'
import json, os, sys
p, new = sys.argv[1], sys.argv[2]
d = {}
if os.path.exists(p):
    with open(p) as f:
        d = json.load(f) or {}
d["data-root"] = new
os.makedirs(os.path.dirname(p), exist_ok=True)
with open(p, "w") as f:
    json.dump(d, f, indent=2, sort_keys=True)
PY
echo "   ${DAEMON}: data-root=${NEW}"
mkdir -p "${DROPIN_DIR}"
printf '[Service]\nEnvironment=STOIC_DOCKER_ROOT=%s\n' "${NEW}" > "${DROPIN_DIR}/20-stoic-docker-root.conf"
# the root-slave drop-in (host-prereqs) points at the old path → re-target it
if [ -f "${DROPIN_DIR}/10-stoic-private-root.conf" ]; then sed -i "s#${OLD}#${NEW}#g" "${DROPIN_DIR}/10-stoic-private-root.conf"; fi
if [ "${MOVED}" = copy ]; then mv "${OLD}" "${OLD}.moved-${TS}"; fi
systemctl daemon-reload
systemctl start docker
for _ in $(seq 1 45); do docker info >/dev/null 2>&1 && break; sleep 2; done
NOW=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)
VOL_AFTER=$(docker volume ls -q 2>/dev/null | sort | tr '\n' ' ')
IMG_AFTER=$(docker image ls -q 2>/dev/null | sort -u | wc -l)
if [ "${NOW}" = "${NEW}" ] && [ "${VOL_AFTER}" = "${VOL_BEFORE}" ] && [ "${IMG_AFTER}" = "${IMG_BEFORE}" ]; then
  echo "== docker root is ${NEW}; volumes and images verified identical"
  [ "${MOVED}" = copy ] && echo "   old copy kept at ${OLD}.moved-${TS} — remove after a successful deploy: rm -rf ${OLD}.moved-${TS}"
  echo "   finish with: sudo bash deploy/update.sh <ref>"
  exit 0
fi
echo "!! verification failed (root=${NOW}, volumes/images differ) — ROLLBACK:"
echo "   systemctl stop docker; python3 -c \"import json;p='${DAEMON}';d=json.load(open(p));d.pop('data-root',None);json.dump(d,open(p,'w'),indent=2)\""
if [ "${MOVED}" = rename ]; then echo "   mv ${NEW} ${OLD}"; else echo "   mv ${OLD}.moved-${TS} ${OLD}"; fi
echo "   rm -f ${DROPIN_DIR}/20-stoic-docker-root.conf; systemctl daemon-reload; systemctl start docker"
exit 1
