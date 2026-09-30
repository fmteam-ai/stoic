#!/usr/bin/env bash
# Restart / recreate one or more compose services through the zombie reaper
# (deploy/lib.sh compose_up). On cPanel hosts a plain `docker compose up
# --force-recreate` fails with "device or resource busy" whenever php-fpm or
# other sandboxed services have cloned the container's rootfs mount since it
# started — this wrapper detaches those copies, reaps, and retries.
#
#   sudo bash deploy/restart.sh backend            # recreate backend (picks up backend/.env changes)
#   sudo bash deploy/restart.sh worker-trading worker-model
#   sudo bash deploy/restart.sh --all
set -euo pipefail
cd "$(dirname "$0")/.."
. deploy/lib.sh
[ "$#" -gt 0 ] || { echo "usage: deploy/restart.sh <service…> | --all"; exit 2; }
if [ "$1" = "--all" ]; then set --; fi
compose_up --force-recreate "$@"
docker compose ps --format 'table {{.Name}}\t{{.Status}}'
