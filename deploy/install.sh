#!/usr/bin/env bash
# STOIC one-command install — builds and starts the full stack (API + 6
# workers + Mongo + frontend [+ TLS ingress]) with generated secrets.
# Idempotent. A deployment MODE is required — no accidental public deploys:
#   deploy/install.sh --dev                     local/dev (loopback-only ports)
#   deploy/install.sh --production <domain>     public TLS deployment via Caddy
#   … --with-forecast                           OPT-IN Chronos/GBM forecast
#                                               profile (≥16 GB RAM hosts)
set -euo pipefail
cd "$(dirname "$0")/.."

WITH_FORECAST=0
REGISTRY=0
CLOUDFLARE=0
CF_ONLY=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --with-forecast) WITH_FORECAST=1 ;;
    --registry) REGISTRY=1 ;;
    --cloudflare) CLOUDFLARE=1 ;;
    --cf-only) CF_ONLY=1 ;;
    --unlock) ;;   # handled below (install lock)
    *) ARGS+=("$a") ;;
  esac
done
MODE="${ARGS[0]:-}"
DOMAIN="${ARGS[1]:-}"
if [ -n "${DOMAIN}" ] && ! [[ "${DOMAIN}" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+$ ]]; then
  echo "ERROR: '${DOMAIN}' is not a valid hostname"; exit 1
fi
case "${MODE}" in
  --dev) ;;
  --production)
    [ -n "${DOMAIN}" ] || { echo "ERROR: --production requires a domain: deploy/install.sh --production trade.example.com"; exit 1; }
    ;;
  --behind-proxy)
    # production hardening, but TLS is terminated by an EXISTING web server on
    # this host (Apache/cPanel, nginx, Plesk) that reverse-proxies to loopback.
    [ -n "${DOMAIN}" ] || { echo "ERROR: --behind-proxy requires a domain: deploy/install.sh --behind-proxy trade.example.com"; exit 1; }
    ;;
  *)
    echo "ERROR: deployment mode is required (prevents accidental public deployment"
    echo "       in development configuration):"
    echo "         deploy/install.sh --dev"
    echo "         deploy/install.sh --production <domain>"
    echo "         deploy/install.sh --behind-proxy <domain>   (Apache/nginx already on 80/443)"
    exit 1
    ;;
esac

TAG=""; [ "${CLOUDFLARE}" = 1 ] && TAG=" · cloudflare origin CA"; [ "${CF_ONLY}" = 1 ] && TAG="${TAG} · cf-only"
echo "== STOIC installer (${MODE#--}${DOMAIN:+ · $DOMAIN}${TAG}) =="

# install lock — written by deploy/bootstrap.sh after a successful install. A
# re-run rebuilds and restarts everything; refuse unless deliberately unlocked
# (bootstrap.sh --unlock, or STOIC_INSTALL_UNLOCK=1 / --unlock for a manual run).
UNLOCK_ARG=0; for a in "$@"; do [ "$a" = "--unlock" ] && UNLOCK_ARG=1; done
if [ -f .stoic-installed ] && [ "${STOIC_INSTALL_UNLOCK:-0}" != 1 ] && [ "${UNLOCK_ARG}" != 1 ]; then
  echo "!! STOIC is already installed here — the installer is LOCKED (.stoic-installed):"
  sed 's/^/   /' .stoic-installed
  echo "   upgrades: deploy/update.sh [tag|sha] · reinstall on purpose: deploy/bootstrap.sh ... --unlock"
  exit 3
fi

if [ "${CLOUDFLARE}" = 1 ]; then
  [ "${MODE}" = "--production" ] || { echo "ERROR: --cloudflare needs --production <domain> (Caddy terminates TLS with the Origin CA certificate)"; exit 1; }
  for f in secrets/origin_cert.pem secrets/origin_key.pem; do
    [ -s "$f" ] || { echo "ERROR: ${f} missing — Cloudflare dashboard → SSL/TLS → Origin Server → Create Certificate, then"
                     echo "       bootstrap.sh --cloudflare ${DOMAIN} --origin-cert <cert.pem> --origin-key <key.pem>"; exit 1; }
  done
  openssl x509 -in secrets/origin_cert.pem -noout >/dev/null 2>&1 || { echo "ERROR: secrets/origin_cert.pem is not a PEM certificate"; exit 1; }
  [ "$(openssl x509 -in secrets/origin_cert.pem -noout -pubkey 2>/dev/null | openssl sha256)" = "$(openssl pkey -in secrets/origin_key.pem -pubout 2>/dev/null | openssl sha256)" ] \
    || { echo "ERROR: secrets/origin_key.pem does not match secrets/origin_cert.pem"; exit 1; }
  SANS=$(openssl x509 -in secrets/origin_cert.pem -noout -ext subjectAltName 2>/dev/null | tr ',' '\n' | sed -n 's/.*DNS:\([^ ]*\).*/\1/p')
  PARENT="${DOMAIN#*.}"
  echo "${SANS}" | grep -qxE "(${DOMAIN}|\*\.${PARENT})" || { echo "ERROR: origin certificate does not cover ${DOMAIN} (SANs: $(echo ${SANS} | tr '\n' ' '))"; exit 1; }
  echo "${SANS}" | grep -qxE "(www\.${DOMAIN}|\*\.${DOMAIN})" || echo "!! origin certificate does not cover www.${DOMAIN} — Cloudflare 'Full (strict)' will fail for the www host"
  echo "-- origin certificate: $(openssl x509 -in secrets/origin_cert.pem -noout -issuer | sed 's/^issuer=//') · expires $(openssl x509 -in secrets/origin_cert.pem -noout -enddate | cut -d= -f2)"
fi

command -v docker >/dev/null || { echo "ERROR: docker is required"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "ERROR: docker compose v2 is required"; exit 1; }

gen() { python3 -c "import secrets;print(secrets.token_urlsafe(32))"; }

# The installer is SELF-CONTAINED: if the .env.example templates are missing
# from the archive, equivalent templates are written inline so a fresh
# deployment never fails on a packaging gap.
ensure_root_template() {
  [ -f .env.example ] && return
  echo "   (writing missing .env.example inline)"
  cat > .env.example <<'EOF'
# Compose-level config — copy to ./.env (repo root). deploy/install.sh
# fills this automatically. NON-SECRET values only: passwords/keys live in
# Docker secrets under ./secrets/ (also generated by install.sh).
MONGO_ROOT_USER=
MONGO_APP_USER=
DB_NAME=
REACT_APP_BACKEND_URL=
EOF
}

ensure_backend_template() {
  [ -f backend/.env.example ] && return
  echo "   (writing missing backend/.env.example inline)"
  cat > backend/.env.example <<'EOF'
# STOIC backend environment — copy to backend/.env and fill in.
MONGO_URL=
DB_NAME=
CORS_ORIGINS=
FRED_API_KEY=
JWT_SECRET=
ADMIN_EMAIL=
ADMIN_PASSWORD=
EMERGENT_LLM_KEY=
ALPHA_VANTAGE_KEY=
NEWSAPI_KEY=
FINNHUB_KEY=
BOT_LOOP_INTERVAL_SEC=
BOT_SIGNAL_COOLDOWN_MIN=
MACRO_FREEZE_BEFORE_MIN=
MACRO_FREEZE_AFTER_MIN=
STRIPE_API_KEY=
BINANCE_LIVE_ENABLED=
CRYPTO_MAX_RISK_PCT_PER_TRADE=
RESEND_API_KEY=
SENDER_EMAIL=
RATE_LIMIT_EXEMPT_IPS=
RATE_LIMIT_BYPASS_TOKEN=
STEP_UP_BYPASS_TOKEN=
KEY_VAULT_MASTER=
BACKGROUND_WORKERS_IN_PROCESS=
METRICS_TOKEN=
WS_ALLOW_QUERY_TOKEN=
EOF
}

set_kv() {  # set_kv <file> <key> <value> — idempotent set-or-append
  if grep -q "^$2=" "$1"; then
    sed -i.bak "s|^$2=.*|$2=$3|" "$1" && rm -f "$1.bak"
  else
    printf '%s=%s\n' "$2" "$3" >> "$1"
  fi
}

# 0 · Docker secrets — all credentials live here, never in .env files
if [ ! -d secrets ]; then
  echo "-- generating Docker secrets in ./secrets/"
  mkdir -p secrets && chmod 700 secrets
  DB_NAME_VAL=ai_trading_bot
  APP_PWD=$(gen)
  gen > secrets/mongo_root_password
  printf '%s' "${APP_PWD}" > secrets/mongo_app_password
  printf 'mongodb://stoic_app:%s@mongo:27017/%s?authSource=%s&replicaSet=rs0' \
    "${APP_PWD}" "${DB_NAME_VAL}" "${DB_NAME_VAL}" > secrets/mongo_url
  gen > secrets/jwt_secret
  gen > secrets/key_vault_master
  gen > secrets/metrics_token
  chmod 600 secrets/*
  echo "   generated mongo_root_password, mongo_app_password, mongo_url,"
  echo "   jwt_secret, key_vault_master, metrics_token (mode 600)."
else
  echo "-- ./secrets exists — leaving untouched"
fi

# 0b · secrets added by later releases — generated when missing (upgrade-safe).
# Key separation: ORDER_AUTH / LEDGER_ANCHOR are distinct from JWT (boot rule).
[ -f secrets/order_auth_secret ] || { gen > secrets/order_auth_secret; echo "   generated order_auth_secret"; }
[ -f secrets/ledger_anchor_key ] || { gen > secrets/ledger_anchor_key; echo "   generated ledger_anchor_key"; }
# MongoDB single-node replica set (transactions are required in production):
# cluster keyFile + replicaSet in the app URL — idempotent, upgrade-safe.
[ -s secrets/mongo_keyfile ] || { openssl rand -base64 756 | tr -d '\n' > secrets/mongo_keyfile; echo "   generated mongo_keyfile (replica set rs0)"; }
grep -q 'replicaSet=' secrets/mongo_url || { printf '&replicaSet=rs0' >> secrets/mongo_url; echo "   mongo_url: added replicaSet=rs0"; }
# Release signer sidecar: private key + bearer token + self-signed TLS cert.
# The API only ever sees the PUBLIC key, the token and the certificate.
ensure_signer_secrets() {
  # Ed25519 via openssl (≥1.1.1) — no Python packages needed on the host.
  # PKCS#8 / SubjectPublicKeyInfo DER both END with the 32 raw key bytes.
  [ -f secrets/signer_ed25519_key ] || {
    openssl genpkey -algorithm ed25519 -out secrets/signer_ed25519_key.pem 2>/dev/null \
      || { echo "ERROR: openssl cannot generate Ed25519 keys (need OpenSSL >= 1.1.1)"; exit 1; }
    openssl pkey -in secrets/signer_ed25519_key.pem -outform DER | tail -c 32 | base64 -w0 > secrets/signer_ed25519_key
    openssl pkey -in secrets/signer_ed25519_key.pem -pubout -outform DER | tail -c 32 | base64 -w0 > secrets/signer_public_key
    rm -f secrets/signer_ed25519_key.pem
    echo "   generated signer_ed25519_key (+ signer_public_key)"; }
  [ -f secrets/signer_token ] || { gen > secrets/signer_token; echo "   generated signer_token"; }
  [ -f secrets/signer_cert.pem ] || {
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
      -subj "/CN=signer" -addext "subjectAltName=DNS:signer,DNS:localhost" \
      -keyout secrets/signer_cert_key.pem -out secrets/signer_cert.pem >/dev/null 2>&1
    echo "   generated signer TLS certificate (CN=signer, 10y, pinned by the API)"; }
  chmod 600 secrets/*
  # SELinux (RHEL/AlmaLinux): containers may read the secret files only when labelled
  if command -v getenforce >/dev/null && [ "$(getenforce 2>/dev/null)" = "Enforcing" ]; then
    chcon -Rt container_file_t secrets 2>/dev/null || true
  fi
}
ensure_signer_secrets
SIGNER_PUB_B64=$(cat secrets/signer_public_key)

# 1 · compose-level .env — NON-SECRET config only
if [ ! -f .env ]; then
  echo "-- creating ./.env (compose config)"
  ensure_root_template
  cp .env.example .env
fi
set_kv .env MONGO_ROOT_USER stoic_root
set_kv .env MONGO_APP_USER stoic_app
set_kv .env DB_NAME ai_trading_bot
if [ "${MODE}" = "--production" ]; then
  set_kv .env DOMAIN "${DOMAIN}"
  set_kv .env REACT_APP_BACKEND_URL ""      # same-origin behind the TLS ingress
  if [ "${CLOUDFLARE}" = 1 ]; then
    set_kv .env COMPOSE_FILE "docker-compose.yml:docker-compose.tls.yml:docker-compose.cloudflare.yml"
    set_kv .env CLOUDFLARE_MODE true
    set_kv .env CLOUDFLARE_ONLY "$([ "${CF_ONLY}" = 1 ] && echo true || echo false)"
    bash deploy/cloudflare/render.sh "${DOMAIN}" $([ "${CF_ONLY}" = 1 ] && echo --cf-only)
  else
    set_kv .env COMPOSE_FILE "docker-compose.yml:docker-compose.tls.yml"
    set_kv .env CLOUDFLARE_MODE false
  fi
elif [ "${MODE}" = "--behind-proxy" ]; then
  set_kv .env DOMAIN "${DOMAIN}"
  set_kv .env REACT_APP_BACKEND_URL ""      # same-origin behind the host web server
  set_kv .env COMPOSE_FILE "docker-compose.yml"   # no Caddy — host proxy owns 80/443
else
  set_kv .env COMPOSE_FILE "docker-compose.yml"
fi
if [ "${WITH_FORECAST}" = "1" ]; then
  echo "-- forecast profile ENABLED (torch CPU + Chronos-Bolt; ~+1.5 GB image)"
  CF=$(grep '^COMPOSE_FILE=' .env | cut -d= -f2-)
  case ":${CF}:" in
    *:docker-compose.forecast.yml:*) ;;
    *) set_kv .env COMPOSE_FILE "${CF}:docker-compose.forecast.yml" ;;
  esac
  set_kv .env ML_FORECAST 1
fi
if [ "${REGISTRY}" = "1" ]; then
  echo "-- deploy mode: REGISTRY (pull CI-built GHCR images by attested digest — no local build)"
  set_kv .env DEPLOY_MODE registry
fi

# 2 · backend/.env — non-secret config (credentials arrive via Docker secrets)
if [ ! -f backend/.env ]; then
  echo "-- creating backend/.env"
  ensure_backend_template
  cp backend/.env.example backend/.env
  set_kv backend/.env DB_NAME ai_trading_bot
  set_kv backend/.env ADMIN_EMAIL admin@stoic.local
  set_kv backend/.env ADMIN_PASSWORD "$(gen)"
  set_kv backend/.env BOT_LOOP_INTERVAL_SEC 30
  set_kv backend/.env BOT_SIGNAL_COOLDOWN_MIN 30
  set_kv backend/.env MACRO_FREEZE_BEFORE_MIN 30
  set_kv backend/.env MACRO_FREEZE_AFTER_MIN 15
  set_kv backend/.env BINANCE_LIVE_ENABLED false
  set_kv backend/.env CRYPTO_MAX_RISK_PCT_PER_TRADE 1.0
  set_kv backend/.env WS_ALLOW_QUERY_TOKEN false
  echo "   seed admin: admin@stoic.local (password in backend/.env — change after first login)"
  echo "   review backend/.env for integration keys (LLM, news, email) before go-live."
else
  echo "-- backend/.env exists — leaving untouched"
fi

# 2b · release signing → external sidecar (every mode). The API env must NOT
# hold the private key; the sidecar's public key is pinned here.
set_kv backend/.env RELEASE_SIGNER external
set_kv backend/.env RELEASE_SIGNER_URL https://signer:9443
set_kv backend/.env RELEASE_SIGNER_ALLOWED_HOSTS signer
set_kv backend/.env RELEASE_SIGNER_KEY_ID stoic-release-ed25519-v1
set_kv backend/.env RELEASE_SIGNER_TIMEOUT 10
set_kv backend/.env RELEASE_PUBLIC_KEY_B64 "${SIGNER_PUB_B64}"
set_kv backend/.env RELEASE_SIGNER_DEFERRED false
sed -i '/^ED25519_SIGNING_KEY_B64=/d; /^RELEASE_SIGNER_TOKEN=/d; /^RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD=/d' backend/.env
# test-only bypass secrets never exist on a server install
sed -i '/^STEP_UP_BYPASS_TOKEN=/d; /^RATE_LIMIT_BYPASS_TOKEN=/d' backend/.env

# 3 · deployment-mode hardening (explicit, not just a warning)
if [ "${MODE}" = "--production" ] || [ "${MODE}" = "--behind-proxy" ]; then
  set_kv backend/.env APP_ENV production
  set_kv backend/.env ADMIN_MFA_ENFORCED true
  set_kv backend/.env CSRF_ENFORCE_ORIGIN true
  set_kv backend/.env CORS_ORIGINS "https://${DOMAIN},https://www.${DOMAIN}"
  set_kv backend/.env TURNSTILE_EXPECTED_HOSTNAMES "${DOMAIN},www.${DOMAIN}"
else
  grep -q "^APP_ENV=production" backend/.env && {
    echo "ERROR: backend/.env says APP_ENV=production but you ran --dev."
    echo "       Re-run with: deploy/install.sh --production <domain>"; exit 1; }
  set_kv backend/.env APP_ENV development
  echo "   DEV MODE: all ports bound to 127.0.0.1 only — not publicly reachable."
fi

# 4 · build + start
echo "-- building images"
# Immutable Git + image provenance (deploy/lib.sh) — the backend image build
# HARD-FAILS without the commit SHA; production boot requires the digest.
. deploy/lib.sh
if [ "${MODE}" = "--production" ] || [ "${MODE}" = "--behind-proxy" ]; then
  # First production install: the checkout must be a signed, attested release.
  # Override consciously with ATTESTATION_REQUIRED=false in ./.env (not advised).
  verify_attestation || { echo "ERROR: release attestation gate failed — refusing production install"; exit 1; }
fi
if [ "$(deploy_mode)" = "registry" ] && [ "${MODE}" != "--production" ]; then
  # registry mode is digest-driven: the attestation is the ONLY source of the digests
  verify_attestation || { echo "ERROR: release attestation gate failed — registry mode cannot resolve images"; exit 1; }
fi
provision_images || exit 1
echo "-- starting stack"
compose_up

# 5 · full release-readiness verification — an install that cannot prove the
#     complete trading topology (HTTPS/frontend, Mongo round trip, all six
#     workers, loop progress, reconciliation, outbox, schema) DOES NOT count
#     as complete.
echo "-- waiting for API health"
for i in $(seq 1 45); do
  if curl -fsS http://127.0.0.1:8001/api/health >/dev/null 2>&1; then
    echo "   API is up"
    break
  fi
  [ "$i" = 45 ] && { echo "ERROR: API did not become healthy"; docker compose logs backend | tail -30; exit 1; }
  sleep 2
done

echo "-- verifying infrastructure readiness (Mongo, 6 workers, loop progress, reconciliation, outbox, schema, ledger anchor, execution truth)"
METRICS_TOKEN=$(cat secrets/metrics_token)
# Infrastructure checks must be green for the install to count. The remaining
# release GATES (inventory approval, EA verification, CI attestation, rc_lock)
# are operator onboarding steps done AFTER install; until they clear, trading is
# fail-closed (CLOSE_ONLY) by design — the installer lists them, it does not
# pretend they can be satisfied on a fresh host.
INFRA="mongo_roundtrip workers loop_progress reconciliation outbox schema repair_ledger_anchor execution_truth"
READY=0; STATE=""
for i in $(seq 1 60); do   # workers need time to acquire leases + first loop iterations
  STATE=$(curl -sS -H "X-Metrics-Token: ${METRICS_TOKEN}" http://127.0.0.1:8001/api/ops/release-readiness 2>/dev/null || true)
  if [ -n "${STATE}" ] && STATE="${STATE}" python3 -c '
import json, os, sys
d = json.loads(os.environ["STATE"]); c = d.get("checks", {})
missing = [k for k in sys.argv[1].split() if not c.get(k, {}).get("ok")]
sys.exit(0 if not missing else 1)' "${INFRA}"; then READY=1; break; fi
  sleep 4
done
if [ "${READY}" != 1 ]; then
  echo "ERROR: infrastructure never became ready — final state:"
  STATE="${STATE}" python3 -c '
import json, os, sys
d = json.loads(os.environ.get("STATE") or "{}"); c = d.get("checks", {})
for k in sys.argv[1].split():
    v = c.get(k, {}); print(("   PASS " if v.get("ok") else "   FAIL ") + k, "" if v.get("ok") else json.dumps({x: v[x] for x in v if x != "detail"})[:300])' "${INFRA}" 2>/dev/null || echo "${STATE}"
  docker compose logs --tail 25
  echo "INSTALL INCOMPLETE — the stack is running but NOT verified. Fix the"
  echo "failing checks above and re-run the installer."
  exit 1
fi
echo "   infrastructure readiness: all green"
STATE="${STATE}" python3 -c '
import json, os
d = json.loads(os.environ["STATE"]); c = d.get("checks", {})
gates = {k: v for k, v in c.items() if isinstance(v, dict) and v.get("ok") is False}
if d.get("ready"):
    print("   release gates: all clear — trading authority can open"); raise SystemExit
print("   release gates pending (trading stays fail-closed / CLOSE_ONLY until cleared — by design):")
hints = {
  "inventory": "declare the 6/3/3 inventory expectation and approve it in Admin → Inventory after adding the MT5 accounts",
  "canonical_decision": "clears automatically once inventory is approved and positions reconcile",
  "ea_release": "compile the RC MQ5 in Windows MetaEditor, then scripts/verify_ea_release.py --sign (docs/RELEASE_SUMMARY.md)",
  "release_attestation": "install from a CI-attested tag without --skip-attestation, or run deploy/lib.sh verify_attestation",
  "rc_lock": "install a tagged release whose release/rc_lock.json matches the running build",
  "turnstile_config": "set TURNSTILE_SITE_KEY/SECRET in backend/.env (or disable the policy) — optional",
}
for k, v in gates.items():
    why = ", ".join(v.get("violations") or v.get("failures") or v.get("reason_codes") or [v.get("note") or v.get("state") or ""])
    print(f"     - {k}: {why[:160]}")
    if k in hints: print(f"         → {hints[k]}")
'

if [ "${MODE}" = "--production" ] && [ "${CLOUDFLARE}" = 1 ]; then
  echo "-- verifying origin TLS (Cloudflare Origin CA certificate served by Caddy on this host)"
  ORIGIN_OK=0
  for i in $(seq 1 12); do
    ISSUER=$(openssl s_client -connect 127.0.0.1:443 -servername "${DOMAIN}" </dev/null 2>/dev/null | openssl x509 -noout -issuer 2>/dev/null || true)
    case "${ISSUER}" in *loud[fF]lare*) ORIGIN_OK=1; break ;; esac
    sleep 5
  done
  [ "${ORIGIN_OK}" = 1 ] || { echo "ERROR: Caddy is not serving the Cloudflare origin certificate on 443 (issuer: ${ISSUER:-none})"; docker compose logs caddy --tail 20 || true; exit 1; }
  echo "   origin certificate served ($(echo "${ISSUER}" | sed 's/^issuer=//'))"
  # direct (non-Cloudflare) peer: cf-only must refuse it, otherwise it must answer
  CODE=$(curl -sk -m 10 -o /dev/null -w '%{http_code}' --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/api/health" 2>/dev/null || true); [ -n "${CODE}" ] || CODE=000
  if [ "${CF_ONLY}" = 1 ]; then
    [ "${CODE}" = 000 ] && echo "   cf-only: direct (non-Cloudflare) connection refused — origin answers only to Cloudflare edges" \
      || { echo "ERROR: --cf-only but a direct connection was served (HTTP ${CODE})"; exit 1; }
  else
    [ "${CODE}" = 200 ] && echo "   origin answers /api/health directly (HTTP ${CODE})" || { echo "ERROR: origin /api/health → HTTP ${CODE}"; exit 1; }
  fi
  echo "-- verifying HTTPS through Cloudflare (https://${DOMAIN} — SSL/TLS mode must be 'Full (strict)')"
  TLS_OK=0
  for i in $(seq 1 12); do
    if curl -fsS -m 15 "https://${DOMAIN}/api/health" >/dev/null 2>&1; then TLS_OK=1; break; fi
    sleep 5
  done
  if [ "${TLS_OK}" != 1 ]; then
    echo "ERROR: https://${DOMAIN}/api/health not reachable through Cloudflare."
    echo "       Check: the record is proxied (orange) and points at this host, SSL/TLS mode is 'Full (strict)',"
    echo "       and the Origin CA certificate covers ${DOMAIN}. Origin side is fine (verified above)."
    curl -sS -m 15 -o /dev/null -w '       edge → HTTP %{http_code}\n' "https://${DOMAIN}/api/health" || true
    exit 1
  fi
  echo "   HTTPS verified end-to-end via Cloudflare"
elif [ "${MODE}" = "--production" ]; then
  echo "-- verifying HTTPS end-to-end (Caddy certificate for ${DOMAIN})"
  TLS_OK=0
  for i in $(seq 1 36); do   # cert issuance can take ~1-2 min after DNS resolves
    if curl -fsS "https://${DOMAIN}/api/health" >/dev/null 2>&1; then
      TLS_OK=1; break
    fi
    sleep 5
  done
  if [ "${TLS_OK}" != 1 ]; then
    echo "ERROR: https://${DOMAIN}/api/health is not reachable with a valid certificate."
    echo "       Check that DNS points at this host and ports 80/443 are open,"
    echo "       then re-run: deploy/install.sh --production ${DOMAIN}"
    docker compose logs caddy --tail 20 || true
    exit 1
  fi
  echo "   HTTPS verified"
else
  echo "-- verifying frontend"
  curl -fsS -o /dev/null http://127.0.0.1:3000 || { echo "ERROR: frontend not reachable on 127.0.0.1:3000"; exit 1; }
  echo "   frontend serving"
fi

echo "-- worker status"
docker compose ps --format '{{.Name}}\t{{.Status}}' | grep worker || true

if [ "${WITH_FORECAST}" = "1" ]; then
  echo "-- verifying forecast profile (loads Chronos-Bolt once; model is cached in the hf_cache volume)"
  if docker compose exec -T worker-trading python ops/verify_forecast_profile.py; then
    echo "   forecast plane READY in worker-trading"
  else
    echo "ERROR: forecast profile verification failed — see output above"; exit 1
  fi
fi

echo ""
echo "== install complete =="
if [ "${MODE}" = "--production" ] && [ "${CLOUDFLARE}" = 1 ]; then
  echo "   app:   https://${DOMAIN}  (Cloudflare Origin CA certificate — record stays proxied/orange; SSL mode 'Full (strict)')"
elif [ "${MODE}" = "--production" ]; then
  echo "   app:   https://${DOMAIN}  (Caddy provisions the certificate on first request)"
elif [ "${MODE}" = "--behind-proxy" ]; then
  bash deploy/proxy/render.sh "${DOMAIN}"
  echo "   app:   https://${DOMAIN}  once your web server proxies to 127.0.0.1:3000 (/) and 127.0.0.1:8001 (/api)"
  echo "          → ready-made vhost snippets: deploy/proxy/apache-${DOMAIN}.conf · deploy/proxy/nginx-${DOMAIN}.conf"
else
  echo "   app:   http://127.0.0.1:3000   (loopback only)"
fi
echo "   next:  deploy/backup.sh schedule · deploy/soak.sh start · docs/DEPLOYMENT.md"
