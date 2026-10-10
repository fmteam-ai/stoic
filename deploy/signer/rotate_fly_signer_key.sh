#!/usr/bin/env bash
# S-1 — ROTATE the CI release signing key on the Fly signer (deliberate ceremony, run from the operator's machine).
#
#   deploy/signer/rotate_fly_signer_key.sh <fly-app> <new-key-id>        e.g.  stoic-signer stoic-release-ed25519-v2
#   options: --keep-token (do not mint a new release bearer token) · --yes (no prompt)
#
# · generates a NEW Ed25519 keypair + (by default) a NEW release bearer token LOCALLY (~/.stoic-signer/<app>.<key-id>.*)
# · refuses when <new-key-id> equals the key id the signer currently serves (never re-key under the same id — N105-2)
# · flyctl secrets set ED25519_SIGNING_KEY_B64 + SIGNER_KEY_ID (+ SIGNER_TOKEN) → the signer redeploys with the new key
# · verifies GET /public-key serves <new-key-id> with the new key, prints the fingerprint line for
#   release/release_key.fingerprint, the GitHub secrets and the server re-pin command (docs/RELEASE_KEY_ROTATION.md)
# The old private key is NOT recoverable from the signer afterwards — that is the point.
set -euo pipefail
cd "$(dirname "$0")"

APP="${1:?usage: rotate_fly_signer_key.sh <fly-app> <new-key-id> [--keep-token] [--yes]}"
NEW_ID="${2:?usage: rotate_fly_signer_key.sh <fly-app> <new-key-id> [--keep-token] [--yes]}"
shift 2
KEEP_TOKEN=0; YES=0
for a in "$@"; do case "$a" in --keep-token) KEEP_TOKEN=1 ;; --yes) YES=1 ;; *) echo "unknown option $a"; exit 64 ;; esac; done
OUT_DIR="${STOIC_SIGNER_DIR:-${HOME}/.stoic-signer}"
FLY="${FLYCTL:-flyctl}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)"; exit 1; }; }
need "${FLY}" "install: curl -L https://fly.io/install.sh | sh"
need python3 "python 3.9+"; need openssl "openssl"; need curl "curl"
python3 -c "import cryptography" 2>/dev/null || { echo "missing python module: pip install cryptography"; exit 1; }
case "${NEW_ID}" in stoic-release-ed25519-v[0-9]*) ;; *) echo "!! new key id must look like stoic-release-ed25519-v<N> (got ${NEW_ID})"; exit 64 ;; esac

URL="${SIGNER_URL:-https://${APP}.fly.dev}"
CUR=$(curl -fsS --max-time 10 "${URL}/public-key" 2>/dev/null || true)
CUR_ID=$(printf '%s' "${CUR}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("key_id",""))' 2>/dev/null || true)
CUR_PUB=$(printf '%s' "${CUR}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("public_key_b64",""))' 2>/dev/null || true)
[ -n "${CUR_ID}" ] || { echo "!! ${URL}/public-key unreachable — refusing to rotate blind (fix the signer first)"; exit 2; }
[ "${CUR_ID}" != "${NEW_ID}" ] || { echo "!! the signer already serves ${NEW_ID} — a rotation needs a NEW key id (never re-key under the same id)"; exit 3; }
[ -s "${OUT_DIR}/${APP}.${NEW_ID}.private.b64" ] && { echo "!! ${OUT_DIR}/${APP}.${NEW_ID}.private.b64 exists — this rotation was already prepared here; refusing to overwrite"; exit 3; }

fp() { printf '%s' "$1" | python3 -c 'import base64,hashlib,sys; k=base64.b64decode(sys.stdin.read().strip()); assert len(k)==32; print("SHA256:"+hashlib.sha256(k).hexdigest())'; }
echo "== rotating the CI release key on ${APP} (${URL})"
echo "   current: ${CUR_ID} $(fp "${CUR_PUB}")"
echo "   new:     ${NEW_ID}"
if [ "${YES}" != 1 ]; then
  read -r -p "   The old key stops signing immediately; every host must re-pin and the EA record/policies must be re-signed. Proceed? [y/N] " ans
  [ "${ans}" = y ] || [ "${ans}" = Y ] || { echo "aborted"; exit 1; }
fi

mkdir -p "${OUT_DIR}"; chmod 700 "${OUT_DIR}"
KEYS=$(python3 - <<'EOF'
import base64
from cryptography.hazmat.primitives import serialization as s
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
k = Ed25519PrivateKey.generate()
print(base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode())
print(base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode())
EOF
)
PRIVATE_B64=$(echo "${KEYS}" | sed -n 1p); PUBLIC_B64=$(echo "${KEYS}" | sed -n 2p)
umask 077
printf '%s\n' "${PRIVATE_B64}" > "${OUT_DIR}/${APP}.${NEW_ID}.private.b64"
printf '%s\n' "${PUBLIC_B64}"  > "${OUT_DIR}/${APP}.${NEW_ID}.public.b64"
SECRETS=(ED25519_SIGNING_KEY_B64="${PRIVATE_B64}" SIGNER_KEY_ID="${NEW_ID}")
TOKEN=""
if [ "${KEEP_TOKEN}" != 1 ]; then
  TOKEN=$(openssl rand -hex 32); printf '%s\n' "${TOKEN}" > "${OUT_DIR}/${APP}.${NEW_ID}.token"
  SECRETS+=(SIGNER_TOKEN="${TOKEN}")
fi
echo "-- flyctl secrets set (the signer restarts with the new key)"
"${FLY}" secrets set -a "${APP}" "${SECRETS[@]}" >/dev/null

echo "-- verifying ${URL}/public-key"
NEW_PUB=""
for _ in $(seq 1 30); do
  body=$(curl -fsS --max-time 10 "${URL}/public-key" 2>/dev/null || true)
  kid=$(printf '%s' "${body}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("key_id",""))' 2>/dev/null || true)
  if [ "${kid}" = "${NEW_ID}" ]; then NEW_PUB=$(printf '%s' "${body}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("public_key_b64",""))'); break; fi
  sleep 4
done
[ "${NEW_PUB}" = "${PUBLIC_B64}" ] || { echo "!! signer does not serve the new key yet (got '${NEW_PUB:-nothing}') — check: ${FLY} status -a ${APP}; ${FLY} logs -a ${APP}"; exit 2; }
NEW_FP=$(fp "${PUBLIC_B64}"); OLD_FP=$(fp "${CUR_PUB}")
echo "   signer OK — ${NEW_ID} ${NEW_FP}"

cat <<EOF

========= 1. repo — release/release_key.fingerprint (commit on main, then tag) =========
${NEW_ID} ${NEW_FP} current created=$(date -u +%Y-%m-%d)
${CUR_ID} ${OLD_FP} transition        # → change to 'revoked' once every host re-pinned and the EA record is re-signed (keep its created=)

========= 2. GitHub → Settings → Secrets and variables → Actions =========
RELEASE_SIGNER_KEY_ID=${NEW_ID}
RELEASE_PUBLIC_KEY_B64=${PUBLIC_B64}
$( [ -n "${TOKEN}" ] && echo "RELEASE_SIGNER_TOKEN=${TOKEN}" || echo "(RELEASE_SIGNER_TOKEN unchanged — --keep-token)" )
Then re-run the ea-release workflow (re-signs release/ea_release.json under ${NEW_ID}) and sign the policy
migrations again (scripts/sign_policy_migration.py) BEFORE cutting the tag.

========= 3. every API host (after the tag that carries the fingerprint is checked out) =========
sudo bash deploy/rotate-release-pin.sh ${NEW_ID}          # pins ${NEW_ID}, keeps ${CUR_ID} as a transition key
# … once the EA record / policies are re-signed and all hosts show the new pin:
sudo bash deploy/rotate-release-pin.sh --revoke ${CUR_ID}  # RELEASE_REVOKED_KEY_IDS += ${CUR_ID}

Local copies (chmod 600): ${OUT_DIR}/${APP}.${NEW_ID}.{private.b64,public.b64$( [ -n "${TOKEN}" ] && echo ',token')}
Round-trip check:  python3 scripts/signer_probe.py --url ${URL} --token-file ${OUT_DIR}/${APP}.${NEW_ID}.token --public-key ${PUBLIC_B64} --key-id ${NEW_ID}
EOF
