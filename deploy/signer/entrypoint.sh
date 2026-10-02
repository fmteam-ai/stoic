#!/bin/sh
# Release signer entrypoint — the signing service NEVER runs as root.
# Compose file secrets are bind-mounted with the host's owner/mode (root, 0600),
# so when started as root we stage private 0400 copies owned by the `signer`
# user, repoint *_FILE env vars and /run/secrets/ arguments at them, then drop
# privileges (setpriv, all capabilities cleared). Started as non-root (Fly.io,
# `docker run --user`), it simply execs the command.
set -eu
if [ "$(id -u)" != 0 ]; then exec "$@"; fi
S=/run/signer-secrets
if [ -d /run/secrets ]; then
  install -d -m 700 -o signer -g signer "$S"
  for f in /run/secrets/*; do
    [ -f "$f" ] && install -m 400 -o signer -g signer "$f" "$S/$(basename "$f")"
  done
  for v in $(env | sed -n 's#^\([A-Za-z0-9_]*_FILE\)=/run/secrets/.*#\1#p'); do
    export "$v=$(printenv "$v" | sed "s#^/run/secrets/#$S/#")"
  done
  for a; do
    shift
    set -- "$@" "$(printf '%s' "$a" | sed "s#^/run/secrets/#$S/#")"
  done
fi
exec setpriv --reuid=signer --regid=signer --init-groups --inh-caps=-all --bounding-set=-all -- "$@"
