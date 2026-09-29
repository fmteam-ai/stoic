#!/bin/sh
# Container start wrapper for the mongo service (mounted read-only by compose).
# mongod refuses a keyFile that is not owned by its own uid with mode 400, and a
# bind-mounted Docker secret keeps the HOST owner — so copy it into place first,
# then hand over to the official entrypoint (root user, first-run init scripts,
# drop to the mongodb user). Runs as a single-node replica set so transactions
# are available: production fails closed without them (audit r17 P0-01).
set -e
install -m 400 -o mongodb -g mongodb /run/secrets/mongo_keyfile /data/configdb/keyfile
exec docker-entrypoint.sh mongod --bind_ip_all --replSet rs0 --keyFile /data/configdb/keyfile "$@"
