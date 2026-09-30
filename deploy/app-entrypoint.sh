#!/bin/sh
# Entrypoint for the API + worker images. Docker secrets are root-only 0600 files
# on the host, but the process must run unprivileged (security-audit hardening).
# So: as root, stage private copies owned by `stoic`, re-point every env var that
# references /run/secrets/… to the copy, then drop privileges with setpriv and
# exec the real command (PID 1 stays the app — signals work as before).
set -e
if [ "$(id -u)" = 0 ] && [ -d /run/secrets ]; then
  S=/run/stoic-secrets
  mkdir -p "$S" && chmod 700 "$S"
  for f in /run/secrets/*; do
    [ -f "$f" ] || continue
    install -m 400 -o stoic -g stoic "$f" "$S/$(basename "$f")"
  done
  chown stoic:stoic "$S"
  for kv in $(env | grep -E '^[A-Za-z_][A-Za-z0-9_]*=/run/secrets/[^/]+$'); do
    k=${kv%%=*}; v=${kv#*=}
    export "$k=$S/${v##*/}"
  done
  exec setpriv --reuid=stoic --regid=stoic --init-groups -- "$@"
fi
exec "$@"
