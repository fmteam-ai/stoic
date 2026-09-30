#!/usr/bin/env bash
# cPanel/WHM wiring for --behind-proxy installs: turn the account's Apache vhost
# into a reverse proxy for STOIC without touching httpd.conf by hand.
#   sudo bash deploy/proxy/cpanel.sh <domain> [--user <cpanel-user>] [--check]
# What it does (idempotent):
#   * finds the cPanel account that owns <domain> (/scripts/whoowns)
#   * installs the SSL include  /etc/apache2/conf.d/userdata/ssl/2_4/<user>/<domain>/stoic.conf
#     and an HTTP include        /etc/apache2/conf.d/userdata/std/2_4/<user>/<domain>/stoic.conf
#     (301 → https, except /.well-known/ so AutoSSL DCV keeps working)
#   * makes sure ea-apache24-mod_proxy_wstunnel is present (WebSockets)
#   * /scripts/ensure_vhost_includes → /scripts/rebuildhttpdconf → httpd -t → restart httpd
#   * verifies https://<domain>/api/health through Apache on this host (bypassing DNS/Cloudflare)
# public_html is left untouched; it is simply no longer served for this domain.
set -euo pipefail
cd "$(dirname "$0")/../.."
D=""; USER_ARG=""; CHECK=0
while [ $# -gt 0 ]; do case "$1" in --user) USER_ARG="$2"; shift 2 ;; --check) CHECK=1; shift ;; *) D="$1"; shift ;; esac; done
[[ "${D}" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+$ ]] || { echo "ERROR: usage: cpanel.sh <domain> [--user <cpanel-user>]"; exit 1; }
[ "$(id -u)" = 0 ] || { echo "ERROR: run as root"; exit 1; }
[ -x /scripts/rebuildhttpdconf ] || { echo "ERROR: this is not a cPanel/WHM host (/scripts/rebuildhttpdconf missing) — use deploy/proxy/apache-${D}.conf by hand"; exit 1; }

CPUSER="${USER_ARG}"
if [ -z "${CPUSER}" ]; then
  CPUSER=$(/scripts/whoowns "${D}" 2>/dev/null | tr -d '[:space:]' || true)
  [ -n "${CPUSER}" ] || { echo "ERROR: no cPanel account owns ${D} — create the domain in WHM first, or pass --user <cpanel-user>"; exit 1; }
fi
[[ "${CPUSER}" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || { echo "ERROR: '${CPUSER}' is not a valid cPanel user"; exit 1; }
id "${CPUSER}" >/dev/null 2>&1 || { echo "ERROR: cPanel user ${CPUSER} does not exist on this host"; exit 1; }
DOCROOT=$(getent passwd "${CPUSER}" | cut -d: -f6)/public_html
echo "-- cPanel: ${D} is owned by ${CPUSER} (docroot ${DOCROOT} — left untouched, no longer served)"

[ -f "deploy/proxy/apache-${D}.conf" ] || bash deploy/proxy/render.sh "${D}" >/dev/null
SSL_DIR="/etc/apache2/conf.d/userdata/ssl/2_4/${CPUSER}/${D}"
STD_DIR="/etc/apache2/conf.d/userdata/std/2_4/${CPUSER}/${D}"

if [ "${CHECK}" = 1 ]; then
  [ -f "${SSL_DIR}/stoic.conf" ] && echo "   ssl include: present (${SSL_DIR}/stoic.conf)" || echo "   ssl include: MISSING"
  [ -f "${STD_DIR}/stoic.conf" ] && echo "   http include: present" || echo "   http include: MISSING"
  httpd -M 2>/dev/null | grep -q proxy_wstunnel && echo "   mod_proxy_wstunnel: loaded" || echo "   mod_proxy_wstunnel: MISSING"
  grep -q "userdata/ssl/2_4/${CPUSER}/${D}" /etc/apache2/conf/httpd.conf 2>/dev/null && echo "   httpd.conf: include line present" || echo "   httpd.conf: include line MISSING (run without --check)"
  exit 0
fi

# WebSockets need mod_proxy_wstunnel (EasyApache 4 package)
if ! httpd -M 2>/dev/null | grep -q proxy_wstunnel; then
  echo "-- installing ea-apache24-mod_proxy_wstunnel (WebSockets)"
  (dnf -y -q install ea-apache24-mod_proxy_wstunnel || yum -y -q install ea-apache24-mod_proxy_wstunnel) >/dev/null
fi
for m in proxy proxy_http headers rewrite; do
  httpd -M 2>/dev/null | grep -q "${m}_module" || { echo "ERROR: Apache module mod_${m} not loaded — enable it in WHM → EasyApache 4"; exit 1; }
done

mkdir -p "${SSL_DIR}" "${STD_DIR}"
{
  echo "# STOIC reverse proxy (installed by deploy/proxy/cpanel.sh) — AutoSSL DCV stays on disk"
  echo "# cPanel ModSecurity answers 406 to proxied API/WebSocket traffic — the app enforces its own request guards"
  echo "<IfModule security2_module>"
  echo "    SecRuleEngine Off"
  echo "</IfModule>"
  echo "ProxyPass /.well-known !"
  # cPanel service shortcuts must keep working on the domain (they redirect to :2083/:2087/:2096)
  for p in /cpanel /whm /webmail /cpanelwebcall /autodiscover /Autodiscover; do echo "ProxyPass ${p} !"; done
  grep -vE '^#' "deploy/proxy/apache-${D}.conf"
} > "${SSL_DIR}/stoic.conf"
cat > "${STD_DIR}/stoic.conf" <<HTTP
# STOIC (installed by deploy/proxy/cpanel.sh): plain HTTP → HTTPS, except AutoSSL/ACME validation
RewriteEngine On
RewriteCond %{REQUEST_URI} !^/\.well-known/
RewriteRule ^ https://%{HTTP_HOST}%{REQUEST_URI} [R=301,L]
HTTP
chmod 644 "${SSL_DIR}/stoic.conf" "${STD_DIR}/stoic.conf"
echo "-- includes written: ${SSL_DIR}/stoic.conf · ${STD_DIR}/stoic.conf"

/scripts/ensure_vhost_includes --user="${CPUSER}" >/dev/null 2>&1 || true
/scripts/rebuildhttpdconf >/dev/null
httpd -t 2>&1 | grep -q "Syntax OK" || { echo "ERROR: httpd -t failed after adding the include:"; httpd -t; rm -f "${SSL_DIR}/stoic.conf" "${STD_DIR}/stoic.conf"; /scripts/rebuildhttpdconf >/dev/null; exit 1; }
/scripts/restartsrv_httpd >/dev/null 2>&1 || systemctl restart httpd
echo "-- httpd.conf rebuilt and httpd restarted"

# verify through Apache on this host, independent of DNS / Cloudflare
sleep 2
CODE=$(curl -sk -m 15 -o /tmp/stoic_cp_health -w '%{http_code}' --resolve "${D}:443:127.0.0.1" "https://${D}/api/health" 2>/dev/null || true); [ -n "${CODE}" ] || CODE=000
if [ "${CODE}" = 200 ] && grep -q '"status"' /tmp/stoic_cp_health 2>/dev/null; then
  echo "-- verified: https://${D}/api/health via Apache → STOIC (HTTP 200)"
else
  HINT="Is the stack up? (docker compose ps; curl -s http://127.0.0.1:8001/api/health)"
  [ "${CODE}" = 406 ] && HINT="406 = cPanel ModSecurity blocked the proxied request although SecRuleEngine Off is in the include — disable ModSecurity for ${D} in WHM → Security Center → ModSecurity Domain Manager, or check /etc/apache2/logs/modsec_audit.log"
  echo "ERROR: https://${D}/api/health via Apache → HTTP ${CODE}. ${HINT}"; exit 1
fi
HCODE=$(curl -s -m 10 -o /dev/null -w '%{http_code}' --resolve "${D}:80:127.0.0.1" "http://${D}/" 2>/dev/null || true)
[ "${HCODE}" = 301 ] && echo "-- verified: http://${D}/ → 301 https" || echo "!! http://${D}/ returned ${HCODE:-000} (expected 301) — check cPanel 'Force HTTPS Redirect' is not conflicting"
echo "-- done. Public cut-over: point the Cloudflare A records for ${D} and www at this host's IP (keep the orange cloud, SSL mode 'Full (strict)')."
