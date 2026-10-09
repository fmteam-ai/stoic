#!/usr/bin/env bash
# M119-1 — rotate the RUNTIME (sidecar) signing key. Needed when the sidecar key doubles as the CI release key
# (RELEASE_PUBLIC_KEY_B64 == secrets/signer_public_key): the host would then hold the CI release PRIVATE key.
#   sudo bash deploy/rotate-runtime-key.sh [--no-restart]
# · new Ed25519 key → secrets/signer_ed25519_key + secrets/signer_public_key (old private key kept 0600 as
#   secrets/signer_ed25519_key.prev-<ts> — DELETE it once nothing needs it)
# · re-pins BUNDLE_PUBLIC_KEY_B64 + bumps BUNDLE_SIGNER_KEY_ID (backend/.env) and SIGNER_KEY_ID (.env)
# · RELEASE_PUBLIC_KEY_B64 / RELEASE_SIGNER_KEY_ID are NEVER touched (they stay the CI release pin)
# · restarts signer + backend + workers so the new key is live (skip with --no-restart)
set -euo pipefail
cd "$(dirname "$0")/.."
. deploy/lib.sh
RESTART=1; [ "${1:-}" = "--no-restart" ] && RESTART=0
[ "$(id -u)" = 0 ] || { echo "rotate-runtime-key: run as root"; exit 1; }
command -v openssl >/dev/null || { echo "rotate-runtime-key: openssl required"; exit 1; }
mkdir -p secrets; chmod 700 secrets
TS=$(date -u +%Y%m%dT%H%M%SZ)
OLD_PUB=$(cat secrets/signer_public_key 2>/dev/null || true)
CUR_ID=$( { grep -E '^BUNDLE_SIGNER_KEY_ID=' backend/.env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d '"'"'")
CUR_ID="${CUR_ID:-stoic-bundle-ed25519-v1}"
N="${CUR_ID##*-v}"; case "${N}" in ''|*[!0-9]*) N=1 ;; esac
NEW_ID="${CUR_ID%-v*}-v$((N + 1))"

tmp=$(mktemp -d); trap 'rm -rf "${tmp}"' EXIT
openssl genpkey -algorithm ed25519 -out "${tmp}/k.pem" 2>/dev/null
openssl pkey -in "${tmp}/k.pem" -outform DER | tail -c 32 | base64 -w0 > "${tmp}/priv"
openssl pkey -in "${tmp}/k.pem" -pubout -outform DER | tail -c 32 | base64 -w0 > "${tmp}/pub"
NEW_PUB=$(cat "${tmp}/pub")
[ "${NEW_PUB}" != "${OLD_PUB}" ] || { echo "rotate-runtime-key: generated key equals the current one (?)"; exit 1; }

if [ -f secrets/signer_ed25519_key ]; then
  mv secrets/signer_ed25519_key "secrets/signer_ed25519_key.prev-${TS}"; chmod 600 "secrets/signer_ed25519_key.prev-${TS}"
fi
[ -f secrets/signer_public_key ] && cp secrets/signer_public_key "secrets/signer_public_key.prev-${TS}"
install -m 600 "${tmp}/priv" secrets/signer_ed25519_key
install -m 644 "${tmp}/pub" secrets/signer_public_key
set_kv backend/.env BUNDLE_PUBLIC_KEY_B64 "${NEW_PUB}"
set_kv backend/.env BUNDLE_SIGNER_KEY_ID "${NEW_ID}"
set_kv .env SIGNER_KEY_ID "${NEW_ID}"
echo "rotate-runtime-key: new runtime key ${NEW_ID} pinned (BUNDLE_PUBLIC_KEY_B64); RELEASE_PUBLIC_KEY_B64 untouched"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD 2>/dev/null || echo '?') runtime-key-rotated ${CUR_ID} -> ${NEW_ID}" >> deploy/releases.log

REL=$( { grep -E '^RELEASE_PUBLIC_KEY_B64=' backend/.env 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d '"'"'")
if [ -n "${OLD_PUB}" ] && [ "${OLD_PUB}" = "${REL}" ]; then
  echo "!! SECURITY: the previous sidecar key IS the CI release key — secrets/signer_ed25519_key.prev-${TS} is the CI release PRIVATE key."
  echo "   Rotate the CI release key (docs/RELEASE_SIGNER.md: new RELEASE_SIGNER secret → release/release_key.fingerprint → next tag), then"
  echo "   shred -u secrets/signer_ed25519_key.prev-${TS}   — doctor.sh reports the finding until the file is gone."
fi
if [ "${RESTART}" = 1 ] && docker compose ps -q signer >/dev/null 2>&1; then
  echo "-- restarting signer + backend + workers with the new key"
  docker compose up -d --force-recreate --no-deps signer >/dev/null 2>&1 || true
  docker compose restart backend worker-trading worker-protection worker-reconciliation worker-analytics worker-security worker-model worker-tuning >/dev/null 2>&1 || true
  echo "   done — verify: docker compose exec -T signer python -c 'print(1)' ; deploy/doctor.sh"
fi
