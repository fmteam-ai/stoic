#!/usr/bin/env bash
# S-1 — re-pin the CI release key on an API host after a rotation (docs/RELEASE_KEY_ROTATION.md).
#
#   sudo bash deploy/rotate-release-pin.sh <new-key-id> [--no-restart]
#       RELEASE_SIGNER_KEY_ID=<new-key-id>; the previous id + public key move to RELEASE_ACCEPTED_KEY_IDS /
#       RELEASE_TRANSITION_PUBLIC_KEYS (signatures made before the rotation still verify); RELEASE_PUBLIC_KEY_B64
#       is fetched from the public signer and accepted ONLY when its fingerprint equals the committed
#       release/release_key.fingerprint line marked `current` (deploy/preflight.sh ensure_release_public_key_pin).
#   sudo bash deploy/rotate-release-pin.sh --revoke <old-key-id> [--no-restart]
#       finishes the rotation: <old-key-id> leaves the transition lists and joins RELEASE_REVOKED_KEY_IDS.
#   sudo bash deploy/rotate-release-pin.sh --status
# Never touches BUNDLE_* (the runtime sidecar key — deploy/rotate-runtime-key.sh) or secrets/.
set -euo pipefail
cd "$(dirname "$0")/.."
. deploy/lib.sh
. deploy/preflight.sh

ENV_FILE="${STOIC_BACKEND_ENV:-backend/.env}"
envv() { { grep -E "^$1=" "${ENV_FILE}" 2>/dev/null || true; } | head -1 | cut -d= -f2- | tr -d '"'"'"; }
csv_add() { python3 -c 'import sys; cur=[x.strip() for x in sys.argv[1].split(",") if x.strip()]; [cur.append(a) for a in sys.argv[2:] if a and a not in cur]; print(",".join(cur))' "$@"; }
csv_del() { python3 -c 'import sys; print(",".join(x.strip() for x in sys.argv[1].split(",") if x.strip() and x.strip().split("=")[0] != sys.argv[2]))' "$@"; }

status() {
  echo "release key pin on this host (${ENV_FILE}):"
  echo "   RELEASE_SIGNER_KEY_ID            $(envv RELEASE_SIGNER_KEY_ID)"
  local pin; pin=$(envv RELEASE_PUBLIC_KEY_B64)
  echo "   RELEASE_PUBLIC_KEY_B64           $( [ -n "${pin}" ] && key_fingerprint "${pin}" || echo '(empty)')"
  echo "   RELEASE_ACCEPTED_KEY_IDS         $(envv RELEASE_ACCEPTED_KEY_IDS)"
  echo "   RELEASE_TRANSITION_PUBLIC_KEYS   $(envv RELEASE_TRANSITION_PUBLIC_KEYS | sed -E 's/=[A-Za-z0-9+\/=]+/=…/g')"
  echo "   RELEASE_REVOKED_KEY_IDS          $(envv RELEASE_REVOKED_KEY_IDS)"
  echo "   repo current key id              $(current_release_key_id)"
}

RESTART=1; MODE=pin; KID=""
for a in "$@"; do
  case "$a" in
    --no-restart) RESTART=0 ;; --revoke) MODE=revoke ;; --status) MODE=status ;;
    -*) echo "unknown option $a"; exit 64 ;; *) KID="$a" ;;
  esac
done
[ "${MODE}" = status ] && { status; exit 0; }
[ "$(id -u)" = 0 ] || { echo "rotate-release-pin: run as root"; exit 1; }
[ -f "${ENV_FILE}" ] || { echo "rotate-release-pin: ${ENV_FILE} missing"; exit 1; }
[ -n "${KID}" ] || { echo "usage: deploy/rotate-release-pin.sh <new-key-id> | --revoke <old-key-id> | --status"; exit 64; }

if [ "${MODE}" = revoke ]; then
  CUR=$(envv RELEASE_SIGNER_KEY_ID)
  [ "${KID}" != "${CUR:-stoic-release-ed25519-v1}" ] || { echo "!! ${KID} is the CURRENT key id — pin the new key first (rotate-release-pin.sh <new-key-id>)"; exit 1; }
  set_kv "${ENV_FILE}" RELEASE_ACCEPTED_KEY_IDS "$(csv_del "$(envv RELEASE_ACCEPTED_KEY_IDS)" "${KID}")"
  set_kv "${ENV_FILE}" RELEASE_TRANSITION_PUBLIC_KEYS "$(csv_del "$(envv RELEASE_TRANSITION_PUBLIC_KEYS)" "${KID}")"
  set_kv "${ENV_FILE}" RELEASE_REVOKED_KEY_IDS "$(csv_add "$(envv RELEASE_REVOKED_KEY_IDS)" "${KID}")"
  echo "rotate-release-pin: ${KID} REVOKED — signatures under it no longer verify on this host"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD 2>/dev/null || echo '?') release-key-revoked ${KID}" >> deploy/releases.log
  repair_journal release_key_revoked "${KID}" 2>/dev/null || true
else
  REPO_CUR=$(current_release_key_id)
  [ -z "${REPO_CUR}" ] || [ "${REPO_CUR}" = "${KID}" ] || { echo "!! repo marks ${REPO_CUR} as the current key, not ${KID} — check out the release that carries the rotation first"; exit 1; }
  [ -n "$(expected_release_fingerprint "${KID}")" ] || { echo "!! no committed fingerprint for ${KID} in ${RELEASE_KEY_FINGERPRINT_FILE} — refusing (the repo, not the network, decides which key is trusted)"; exit 1; }
  [ "$(release_key_status "${KID}")" != revoked ] || { echo "!! ${KID} is revoked in ${RELEASE_KEY_FINGERPRINT_FILE}"; exit 1; }
  OLD_ID=$(envv RELEASE_SIGNER_KEY_ID); OLD_ID="${OLD_ID:-stoic-release-ed25519-v1}"
  OLD_PUB=$(envv RELEASE_PUBLIC_KEY_B64)
  [ "${OLD_ID}" != "${KID}" ] || { echo "rotate-release-pin: ${KID} is already the pinned key id"; status; exit 0; }
  if [ -n "${OLD_PUB}" ] && [ "$(release_key_status "${OLD_ID}")" != revoked ]; then
    set_kv "${ENV_FILE}" RELEASE_ACCEPTED_KEY_IDS "$(csv_add "$(envv RELEASE_ACCEPTED_KEY_IDS)" "${OLD_ID}")"
    set_kv "${ENV_FILE}" RELEASE_TRANSITION_PUBLIC_KEYS "$(csv_add "$(envv RELEASE_TRANSITION_PUBLIC_KEYS)" "${OLD_ID}=${OLD_PUB}")"
    echo "rotate-release-pin: ${OLD_ID} kept as a TRANSITION key (finish with: rotate-release-pin.sh --revoke ${OLD_ID})"
  fi
  set_kv "${ENV_FILE}" RELEASE_SIGNER_KEY_ID "${KID}"
  set_kv "${ENV_FILE}" RELEASE_PUBLIC_KEY_B64 ""
  PREFLIGHT_YES=1 ensure_release_public_key_pin
  [ -n "$(envv RELEASE_PUBLIC_KEY_B64)" ] || { echo "!! ${KID} could not be pinned (see the release key lines above) — RELEASE_PUBLIC_KEY_B64 is EMPTY: CI-signed records do not verify until it is"; exit 2; }
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(git rev-parse --short HEAD 2>/dev/null || echo '?') release-key-repinned ${OLD_ID} -> ${KID}" >> deploy/releases.log
fi
status
if [ "${RESTART}" = 1 ]; then
  echo "-- applying the env change (deploy/restart.sh --env-changed --yes)"
  bash deploy/restart.sh --env-changed --yes
else
  echo "   apply with: sudo bash deploy/restart.sh --env-changed --yes"
fi
