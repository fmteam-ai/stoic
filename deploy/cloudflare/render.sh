#!/usr/bin/env bash
# Render the Caddyfile for Cloudflare mode (Origin CA certificate, orange cloud).
#   deploy/cloudflare/render.sh <domain> [--cf-only]   → deploy/cloudflare/Caddyfile
# --cf-only: abort any connection whose peer is not a Cloudflare edge (the
# origin then answers only to Cloudflare, even if port 443 is reachable).
# The edge IP list is fetched from cloudflare.com; deploy/cloudflare/ips.txt is
# the offline fallback.
set -euo pipefail
cd "$(dirname "$0")/../.."
D="${1:?domain}"; CF_ONLY=0; [ "${2:-}" = "--cf-only" ] && CF_ONLY=1
[[ "${D}" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+$ ]] || { echo "ERROR: '${D}' is not a valid hostname"; exit 1; }

RANGES=$( { curl -fsS -m 10 https://www.cloudflare.com/ips-v4; echo; curl -fsS -m 10 https://www.cloudflare.com/ips-v6; } 2>/dev/null \
          | grep -E '^[0-9a-fA-F.:]+/[0-9]+$' | sort -u | tr '\n' ' ' || true)
if [ -z "${RANGES}" ]; then
  RANGES=$(grep -E '^[0-9a-fA-F.:]+/[0-9]+$' deploy/cloudflare/ips.txt | tr '\n' ' ')
  echo "-- cloudflare: edge IP list fetch failed, using bundled deploy/cloudflare/ips.txt ($(echo "${RANGES}" | wc -w) ranges)"
else
  printf '%s\n' ${RANGES} > deploy/cloudflare/ips.txt      # refresh the offline copy
  echo "-- cloudflare: $(echo "${RANGES}" | wc -w) edge IP ranges fetched"
fi
RANGES="${RANGES% }"

{
  echo "# rendered by deploy/cloudflare/render.sh for ${D} — do not edit (re-run the installer)"
  echo "{"
  echo "        servers {"
  echo "                trusted_proxies static ${RANGES}"
  # {client_ip} = CF-Connecting-IP when the peer is a trusted Cloudflare edge,
  # else the socket peer (headers from untrusted peers are ignored by Caddy)
  echo "                client_ip_headers CF-Connecting-IP"
  echo "        }"
  echo "}"
  echo "${D}, www.${D} {"
  echo "        tls /run/secrets/origin_cert /run/secrets/origin_key"
  if [ "${CF_ONLY}" = 1 ]; then
    echo "        @notcf not remote_ip ${RANGES}"
    echo "        abort @notcf"
  fi
  echo "        encode gzip"
  echo "        reverse_proxy frontend:3000 {"
  echo "                header_up X-Forwarded-For {client_ip}"
  echo "                header_up X-Real-IP {client_ip}"
  echo "        }"
  echo "}"
} > deploy/cloudflare/Caddyfile
echo "   rendered deploy/cloudflare/Caddyfile (cf-only=${CF_ONLY})"
