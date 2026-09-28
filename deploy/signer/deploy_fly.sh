#!/usr/bin/env bash
# One-command Fly.io deploy of the isolated STOIC release signer.
#
#   ./deploy_fly.sh [app-name] [region]
#
# Generates a fresh Ed25519 keypair + bearer token LOCALLY, stores the private
# key and token ONLY as Fly secrets, deploys, verifies /healthz + /public-key,
# and prints the exact Secrets-tab block for the API deployment.
# The private key is never written to disk outside ~/.stoic-signer/ (chmod 600).
set -euo pipefail
cd "$(dirname "$0")"

APP="${1:-stoic-signer-$(openssl rand -hex 3)}"
REGION="${2:-ams}"
OUT_DIR="${HOME}/.stoic-signer"
KEY_ID="stoic-release-ed25519-v1"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)"; exit 1; }; }
need flyctl "install: curl -L https://fly.io/install.sh | sh"
need python3 "python 3.10+"
need openssl "openssl"
need curl "curl"
python3 -c "import cryptography" 2>/dev/null || { echo "missing python module: pip install cryptography"; exit 1; }

flyctl auth whoami >/dev/null 2>&1 || { echo "run: flyctl auth login"; exit 1; }

mkdir -p "$OUT_DIR"; chmod 700 "$OUT_DIR"
KEYS=$(python3 - <<'EOF'
import base64
from cryptography.hazmat.primitives import serialization as s
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
k = Ed25519PrivateKey.generate()
priv = base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode()
pub = base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode()
print(priv); print(pub)
EOF
)
PRIVATE_B64=$(echo "$KEYS" | sed -n 1p)
PUBLIC_B64=$(echo "$KEYS" | sed -n 2p)
TOKEN=$(openssl rand -hex 32)

umask 077
printf '%s\n' "$PRIVATE_B64" > "$OUT_DIR/$APP.private.b64"
printf '%s\n' "$TOKEN" > "$OUT_DIR/$APP.token"
printf '%s\n' "$PUBLIC_B64" > "$OUT_DIR/$APP.public.b64"

sed -i.bak "s/^app = .*/app = \"$APP\"/; s/^primary_region = .*/primary_region = \"$REGION\"/" fly.toml && rm -f fly.toml.bak

if ! flyctl apps list --json 2>/dev/null | grep -q "\"Name\": *\"$APP\""; then
  flyctl apps create "$APP" --machines >/dev/null
fi
flyctl secrets set -a "$APP" --stage \
  SIGNER_TOKEN="$TOKEN" \
  ED25519_SIGNING_KEY_B64="$PRIVATE_B64" >/dev/null
flyctl deploy -a "$APP" --ha=false --yes

HOST="$APP.fly.dev"
URL="https://$HOST"
echo; echo "== verifying $URL =="
for i in $(seq 1 20); do
  if curl -fsS "$URL/healthz" >/dev/null 2>&1; then break; fi; sleep 3
done
curl -fsS "$URL/healthz"; echo
REMOTE_PUB=$(curl -fsS "$URL/public-key" | python3 -c "import sys,json;print(json.load(sys.stdin)['public_key_b64'])")
if [ "$REMOTE_PUB" != "$PUBLIC_B64" ]; then
  echo "PUBLIC KEY MISMATCH: signer returned $REMOTE_PUB"; exit 2
fi
HEALTH=$(curl -fsS -H "Authorization: Bearer $TOKEN" "$URL/health")
echo "$HEALTH" | grep -q '"ok":true' || { echo "authenticated /health failed: $HEALTH"; exit 2; }
echo "signer OK — public key pinned: $PUBLIC_B64"

cat <<EOF

================ API deployment → Secrets tab (copy exactly) ================
APP_ENV=production
ADMIN_MFA_ENFORCED=true
RELEASE_SIGNER=external
RELEASE_SIGNER_URL=$URL
RELEASE_SIGNER_ALLOWED_HOSTS=$HOST
RELEASE_SIGNER_TOKEN=$TOKEN
RELEASE_SIGNER_KEY_ID=$KEY_ID
RELEASE_PUBLIC_KEY_B64=$PUBLIC_B64
RELEASE_SIGNER_TIMEOUT=10

DELETE (or leave empty) in the API deployment:
STEP_UP_BYPASS_TOKEN
RATE_LIMIT_BYPASS_TOKEN
ED25519_SIGNING_KEY_B64
RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD
=============================================================================
Signer host URL to hand back: $URL
Local copies (chmod 600): $OUT_DIR/$APP.{private.b64,token,public.b64}

Round-trip check from any machine:
  python3 scripts/signer_probe.py --url $URL --token-file $OUT_DIR/$APP.token --public-key $PUBLIC_B64
EOF
