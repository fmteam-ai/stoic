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
  echo "        }"
  echo "}"
  echo "${D}, www.${D} {"
  echo "        tls /run/secrets/origin_cert /run/secrets/origin_key"
  if [ "${CF_ONLY}" = 1 ]; then
    echo "        @notcf not remote_ip ${RANGES}"
    echo "        abort @notcf"
  fi
  echo "        encode gzip"
  echo "        # daily report S5 — same edge header guarantee as deploy/Caddyfile (\`?\` = only when upstream lacks it; CSP mirrors deploy/nginx.conf)"
  echo "        header {"
  echo "                ?Strict-Transport-Security \"max-age=31536000; includeSubDomains\""
  echo "                ?Content-Security-Policy \"default-src 'self'; script-src 'self' https://challenges.cloudflare.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https:; connect-src 'self' wss: https://challenges.cloudflare.com; frame-src https://challenges.cloudflare.com; frame-ancestors 'none'\""
  echo "                ?X-Frame-Options \"DENY\""
  echo "                ?X-Content-Type-Options \"nosniff\""
  echo "                ?Referrer-Policy \"strict-origin-when-cross-origin\""
  echo "        }"
  echo "        reverse_proxy frontend:3000"
  echo "}"
} > deploy/cloudflare/Caddyfile
echo "   rendered deploy/cloudflare/Caddyfile (cf-only=${CF_ONLY})"
