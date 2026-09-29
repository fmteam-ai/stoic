#!/usr/bin/env bash
# STOIC signed install report — written after every successful bootstrap.
#   deploy/install_report.sh [--email <addr>]
# Produces deploy/releases/install-report-<ts>.json:
#   { report: {host, domain, mode, commit, image_digest, signer_key_id,
#              signer_public_key_b64, doctor: {fail, warn}, ...},
#     signature: {alg: Ed25519, key_id, sig_hex} }
# The report is signed INSIDE the signer sidecar (the private key never leaves
# it) and can be verified with the pinned RELEASE_PUBLIC_KEY_B64. It is then
# e-mailed via Resend (RESEND_API_KEY) to --email / INSTALL_REPORT_EMAIL /
# ADMIN_EMAIL and posted to Telegram when HEALTHWATCH_TELEGRAM_* are set.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
TO=""
while [ $# -gt 0 ]; do case "$1" in --email) TO="$2"; shift 2 ;; *) shift ;; esac; done
envval() { for f in .env backend/.env; do v=$(grep -E "^$1=" "$f" 2>/dev/null | head -1 | cut -d= -f2-); [ -n "$v" ] && { printf '%s' "$v"; return; }; done; printf '%s' "${!1:-}"; }

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ); FTS=$(date -u +%Y%m%dT%H%M%SZ)
HOST=$(hostname -f 2>/dev/null || hostname); DOMAIN=$(envval DOMAIN); MODE=$(envval APP_ENV)
COMMIT=$(git rev-parse HEAD 2>/dev/null || echo unknown)
DIGEST=$(envval STOIC_IMAGE_DIGEST)
KEY_ID=$(envval RELEASE_SIGNER_KEY_ID); [ -n "${KEY_ID}" ] || KEY_ID="stoic-release-ed25519-v1"
PUB=$(envval RELEASE_PUBLIC_KEY_B64)
LOCKED=false; [ -f .stoic-installed ] && LOCKED=true
DOCTOR=$(bash deploy/doctor.sh --quiet 2>&1); DRC=$?
FAILS=$(printf '%s' "${DOCTOR}" | grep -c '^  FAIL'); WARNS=$(printf '%s' "${DOCTOR}" | grep -c '^  WARN')
[ -n "${TO}" ] || TO=$(envval INSTALL_REPORT_EMAIL); [ -n "${TO}" ] || TO=$(envval ADMIN_EMAIL)

mkdir -p deploy/releases
REPORT=$(python3 - "${HOST}" "${DOMAIN}" "${MODE}" "${COMMIT}" "${DIGEST}" "${KEY_ID}" "${PUB}" "${FAILS}" "${WARNS}" "${TS}" "${DRC}" "${LOCKED}" <<'PY'
import json, sys
h, d, m, c, dg, k, p, f, w, ts, rc, locked = sys.argv[1:]
print(json.dumps({"kind": "stoic-install-report", "version": 1, "generated_at": ts, "host": h,
                  "domain": d or None, "app_env": m or None, "commit": c, "image_digest": dg or None,
                  "signer_key_id": k, "signer_public_key_b64": p or None,
                  "doctor": {"fail": int(f), "warn": int(w), "ok": rc == "0"},
                  "installer_locked": locked == "true"},
                 sort_keys=True, separators=(",", ":")))
PY
)
# canonical bytes → signed inside the sidecar (key never leaves the container)
SIG=$(printf '%s' "${REPORT}" | docker compose exec -T signer python -c '
import sys, base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(open("/run/secrets/signer_ed25519_key").read().strip()))
print(key.sign(sys.stdin.buffer.read()).hex())' 2>/dev/null || echo "")
OUT="deploy/releases/install-report-${FTS}.json"
python3 - "${REPORT}" "${SIG}" "${KEY_ID}" > "${OUT}" <<'PY'
import json, sys
report, sig, kid = sys.argv[1:]
print(json.dumps({"report": json.loads(report), "report_canonical": report,
                  "signature": {"alg": "Ed25519", "key_id": kid, "sig_hex": sig or None,
                                "verify": "Ed25519(public_key_b64).verify(bytes.fromhex(sig_hex), report_canonical)"}},
                 indent=2))
PY
[ -n "${SIG}" ] && echo "-- install report signed by the sidecar: ${OUT}" || echo "!! install report written UNSIGNED (signer not reachable): ${OUT}"

TEXT="STOIC install report — ${TS}
host:          ${HOST}
app:           ${DOMAIN:+https://${DOMAIN}}${DOMAIN:-loopback (dev)}
mode:          ${MODE:-?}
commit:        ${COMMIT}
image digest:  ${DIGEST:-<not set>}
signer key id: ${KEY_ID}
public key:    ${PUB:-<none>}
doctor:        ${FAILS} FAIL · ${WARNS} WARN $([ "${DRC}" = 0 ] && echo '(healthy)' || echo '(ATTENTION)')
installer:     $([ "${LOCKED}" = true ] && echo 'LOCKED (.stoic-installed — reinstall only with --unlock; upgrades via deploy/update.sh)' || echo 'not locked')
signature:     ${SIG:0:32}${SIG:+…} (Ed25519, ${KEY_ID})
file:          ${ROOT}/${OUT}"
json_escape() { python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))'; }

RESEND_KEY=$(envval RESEND_API_KEY); FROM=$(envval SENDER_EMAIL); [ -n "${FROM}" ] || FROM="onboarding@resend.dev"
if [ -n "${RESEND_KEY}" ] && [ -n "${TO}" ]; then
  if curl -fsS -m 20 -X POST https://api.resend.com/emails -H "Authorization: Bearer ${RESEND_KEY}" -H "Content-Type: application/json" \
      -d "{\"from\":\"STOIC <${FROM}>\",\"to\":[\"${TO}\"],\"subject\":\"STOIC installed on ${HOST}${DOMAIN:+ · ${DOMAIN}} — signed install report\",\"text\":$(printf '%s' "${TEXT}" | json_escape),\"attachments\":[{\"filename\":\"$(basename "${OUT}")\",\"content\":\"$(base64 -w0 "${OUT}")\"}]}" >/dev/null; then
    echo "-- install report e-mailed to ${TO}"
  else echo "!! install report e-mail failed (Resend) — file kept at ${OUT}"; fi
else
  echo "-- e-mail skipped (set RESEND_API_KEY in backend/.env and INSTALL_REPORT_EMAIL/ADMIN_EMAIL in .env)"
fi
TG_TOKEN=$(envval HEALTHWATCH_TELEGRAM_BOT_TOKEN); TG_CHAT=$(envval HEALTHWATCH_TELEGRAM_CHAT_ID)
if [ -n "${TG_TOKEN}" ] && [ -n "${TG_CHAT}" ]; then
  curl -fsS -m 15 -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" -H "Content-Type: application/json" \
    -d "{\"chat_id\":\"${TG_CHAT}\",\"text\":$(printf '%s' "${TEXT}" | json_escape),\"disable_web_page_preview\":true}" >/dev/null \
    && echo "-- install report posted to Telegram" || echo "!! Telegram post failed"
fi
printf '%s\n' "${TEXT}"
