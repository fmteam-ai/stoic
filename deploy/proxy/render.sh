#!/usr/bin/env bash
# Render reverse-proxy vhost snippets for --behind-proxy installs.
#   deploy/proxy/render.sh <domain>   → deploy/proxy/apache-<domain>.conf, nginx-<domain>.conf
# The host web server keeps 80/443 and its own certificate (cPanel AutoSSL,
# certbot, Plesk); STOIC listens on loopback only: frontend 127.0.0.1:3000,
# API 127.0.0.1:8001 (/api, incl. WebSockets).
set -euo pipefail
cd "$(dirname "$0")/../.."
D="${1:?domain}"
cat > "deploy/proxy/apache-${D}.conf" <<APACHE
# STOIC reverse proxy for ${D} — Apache 2.4 (mod_proxy, mod_proxy_http, mod_proxy_wstunnel, mod_headers)
# cPanel/WHM:  /etc/apache2/conf.d/userdata/ssl/2_4/<cpanel-user>/${D}/stoic.conf
#              then: /scripts/rebuildhttpdconf && systemctl restart httpd
# plain httpd: put inside your <VirtualHost *:443> for ${D} in /etc/httpd/conf.d/, then: apachectl -t && systemctl reload httpd
ProxyPreserveHost On
ProxyRequests Off
RequestHeader set X-Forwarded-Proto "https"
RequestHeader set X-Forwarded-Port "443"
# WebSockets (live prices / status stream)
RewriteEngine On
RewriteCond %{HTTP:Upgrade} =websocket [NC]
RewriteRule ^/api/(.*)  ws://127.0.0.1:8001/api/\$1 [P,L]
# API → backend
ProxyPass        /api http://127.0.0.1:8001/api retry=0 timeout=120
ProxyPassReverse /api http://127.0.0.1:8001/api
# everything else → frontend
ProxyPass        /    http://127.0.0.1:3000/ retry=0
ProxyPassReverse /    http://127.0.0.1:3000/
# large statement uploads
LimitRequestBody 8388608
APACHE
cat > "deploy/proxy/nginx-${D}.conf" <<NGINX
# STOIC reverse proxy for ${D} — nginx (inside your server { listen 443 ssl; server_name ${D}; } block)
client_max_body_size 8m;
location /api/ {
    proxy_pass         http://127.0.0.1:8001;
    proxy_http_version 1.1;
    proxy_set_header   Host              \$host;
    proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto https;
    proxy_set_header   Upgrade           \$http_upgrade;
    proxy_set_header   Connection        "upgrade";
    proxy_read_timeout 120s;
}
location / {
    proxy_pass         http://127.0.0.1:3000;
    proxy_set_header   Host              \$host;
    proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto https;
}
NGINX
echo "-- proxy snippets rendered: deploy/proxy/apache-${D}.conf · deploy/proxy/nginx-${D}.conf"
