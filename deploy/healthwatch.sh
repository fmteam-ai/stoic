#!/usr/bin/env bash
# STOIC live health watcher — runs deploy/doctor.sh and alerts on state changes.
#   deploy/healthwatch.sh run        one check (what the systemd timer calls)
#   deploy/healthwatch.sh install    install + enable the hourly systemd timer (root)
#   deploy/healthwatch.sh status     timer status + last result
#   deploy/healthwatch.sh test       send a test alert to the configured channels
#
# Alert channels (read from ./.env or backend/.env, never printed):
#   HEALTHWATCH_TELEGRAM_BOT_TOKEN + HEALTHWATCH_TELEGRAM_CHAT_ID   (Telegram)
#   RESEND_API_KEY + HEALTHWATCH_EMAIL (defaults to ADMIN_EMAIL)      (e-mail)
# Alerts fire on FAIL (once, then every HEALTHWATCH_REMIND_HOURS=6 while still
# failing) and once more on recovery — no hourly spam while healthy.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/stoic-path.sh"   # private python >= 3.9 (bootstrap.sh) — audit H2 guarded PATH prepend
cd "$(dirname "$0")/.."
ROOT=$(pwd)
STATE_DIR="${HEALTHWATCH_STATE_DIR:-/var/lib/stoic}"
STATE="${STATE_DIR}/healthwatch.state"
LAST="${STATE_DIR}/healthwatch.last"

envval() { for f in .env backend/.env; do v=$(grep -E "^$1=" "$f" 2>/dev/null | head -1 | cut -d= -f2-); [ -n "$v" ] && { printf '%s' "$v"; return; }; done; printf '%s' "${!1:-}"; }
DOMAIN=$(envval DOMAIN); HOST=$(hostname -f 2>/dev/null || hostname)
TG_TOKEN=$(envval HEALTHWATCH_TELEGRAM_BOT_TOKEN); TG_CHAT=$(envval HEALTHWATCH_TELEGRAM_CHAT_ID)
RESEND_KEY=$(envval RESEND_API_KEY); MAIL_TO=$(envval HEALTHWATCH_EMAIL); [ -n "${MAIL_TO}" ] || MAIL_TO=$(envval ADMIN_EMAIL)
MAIL_FROM=$(envval SENDER_EMAIL); [ -n "${MAIL_FROM}" ] || MAIL_FROM="onboarding@resend.dev"
REMIND_H=$(envval HEALTHWATCH_REMIND_HOURS); [ -n "${REMIND_H}" ] || REMIND_H=6

json_escape() { python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))'; }

send_telegram() {  # send_telegram <text>
  [ -n "${TG_TOKEN}" ] && [ -n "${TG_CHAT}" ] || return 1
  curl -fsS -m 15 -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\":\"${TG_CHAT}\",\"text\":$(printf '%s' "$1" | json_escape),\"disable_web_page_preview\":true}" >/dev/null
}

send_email() {     # send_email <subject> <text>
  [ -n "${RESEND_KEY}" ] && [ -n "${MAIL_TO}" ] || return 1
  curl -fsS -m 20 -X POST https://api.resend.com/emails -H "Authorization: Bearer ${RESEND_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"from\":\"STOIC <${MAIL_FROM}>\",\"to\":[\"${MAIL_TO}\"],\"subject\":$(printf '%s' "$1" | json_escape),\"text\":$(printf '%s' "$2" | json_escape)}" >/dev/null
}

alert() {          # alert <subject> <body>  → every configured channel; returns 0 if any succeeded
  local sent=1
  send_telegram "$1"$'\n'"$2" && sent=0
  send_email "$1" "$2" && sent=0
  return ${sent}
}

run_check() {
  mkdir -p "${STATE_DIR}"
  local out rc now prev prev_at fails warns subject
  out=$(bash deploy/doctor.sh --quiet 2>&1); rc=$?
  printf '%s\n' "${out}" > "${LAST}"
  fails=$(printf '%s' "${out}" | grep -c '^  FAIL'); warns=$(printf '%s' "${out}" | grep -c '^  WARN')
  now=$(date -u +%s)
  prev="ok"; prev_at=0
  [ -f "${STATE}" ] && { prev=$(cut -d' ' -f1 "${STATE}"); prev_at=$(cut -d' ' -f2 "${STATE}"); }
  local cur; [ "${rc}" = 0 ] && cur=ok || cur=fail
  local body="host: ${HOST}${DOMAIN:+ · https://${DOMAIN}}
time: $(date -u +%Y-%m-%dT%H:%M:%SZ)
result: ${fails} FAIL · ${warns} WARN
$(printf '%s' "${out}" | grep -E '^  (FAIL|WARN)' | head -20)
—
diagnose: sudo bash ${ROOT}/deploy/doctor.sh --bundle"
  if [ "${cur}" = fail ] && { [ "${prev}" != fail ] || [ $((now - prev_at)) -ge $((REMIND_H * 3600)) ]; }; then
    subject="🔴 STOIC health FAIL on ${HOST}: ${fails} failing check(s)"
    alert "${subject}" "${body}" && echo "alert sent: ${subject}" || echo "alert NOT sent (no channel configured): ${subject}"
    printf 'fail %s\n' "${now}" > "${STATE}"
  elif [ "${cur}" = ok ] && [ "${prev}" = fail ]; then
    subject="🟢 STOIC health recovered on ${HOST}"
    alert "${subject}" "${body}" && echo "recovery alert sent" || echo "recovery (no channel configured)"
    printf 'ok %s\n' "${now}" > "${STATE}"
  elif [ "${cur}" = ok ]; then
    printf 'ok %s\n' "${now}" > "${STATE}"
  fi
  echo "healthwatch: ${cur} (${fails} FAIL · ${warns} WARN)"
  return 0
}

install_timer() {
  [ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
  local every="${HEALTHWATCH_EVERY:-hourly}"
  cat > /etc/systemd/system/stoic-healthwatch.service <<EOF
[Unit]
Description=STOIC health watcher (deploy/doctor.sh + alerts)
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=${ROOT}
ExecStart=/usr/bin/env bash ${ROOT}/deploy/healthwatch.sh run
EOF
  cat > /etc/systemd/system/stoic-healthwatch.timer <<EOF
[Unit]
Description=Run STOIC health watcher ${every}

[Timer]
OnBootSec=10min
OnCalendar=${every}
RandomizedDelaySec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now stoic-healthwatch.timer >/dev/null
  echo "-- stoic-healthwatch.timer enabled (${every}); alerts: telegram=$([ -n "${TG_TOKEN}" ] && echo yes || echo no) email=$([ -n "${RESEND_KEY}" ] && [ -n "${MAIL_TO}" ] && echo yes || echo no)"
  [ -n "${TG_TOKEN}" ] || [ -n "${RESEND_KEY}" ] || echo "   set HEALTHWATCH_TELEGRAM_BOT_TOKEN/HEALTHWATCH_TELEGRAM_CHAT_ID (or RESEND_API_KEY + HEALTHWATCH_EMAIL) in ${ROOT}/.env to receive alerts"
}

case "${1:-run}" in
  run) run_check ;;
  install) install_timer ;;
  status) systemctl list-timers stoic-healthwatch.timer --no-pager 2>/dev/null; echo; cat "${STATE}" 2>/dev/null; echo; cat "${LAST}" 2>/dev/null ;;
  test) alert "STOIC healthwatch test from ${HOST}" "channels are configured correctly." && echo "test alert sent" || { echo "no channel configured"; exit 1; } ;;
  *) sed -n 2,12p "$0"; exit 1 ;;
esac
