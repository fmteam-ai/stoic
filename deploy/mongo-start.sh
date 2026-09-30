#!/bin/sh
# Container start wrapper for the mongo service (mounted read-only by compose).
# Docker secrets are root-only 0600 files on the host, but the official image
# drops to its own `mongodb` user BEFORE reading MONGO_INITDB_ROOT_PASSWORD_FILE,
# the init script and the keyFile — so this wrapper (still root) stages private
# copies with the ownership mongod demands, then hands over to the official
# entrypoint. Runs as a single-node replica set so transactions are available:
# production fails closed without them (audit r17 P0-01).
set -e
S=/data/configdb/secrets       # anonymous volume inside the container, never on the host
mkdir -p "$S"
for f in mongo_keyfile mongo_root_password mongo_app_password; do
  [ -s "/run/secrets/$f" ] || { echo "mongo-start: /run/secrets/$f missing or empty" >&2; exit 1; }
  install -m 400 -o mongodb -g mongodb "/run/secrets/$f" "$S/$f"
done
chown mongodb:mongodb "$S" && chmod 700 "$S"
export MONGO_INITDB_ROOT_PASSWORD_FILE="$S/mongo_root_password"
export MONGO_APP_PASSWORD_FILE="$S/mongo_app_password"
exec docker-entrypoint.sh mongod --bind_ip_all --replSet rs0 --keyFile "$S/mongo_keyfile" "$@"
