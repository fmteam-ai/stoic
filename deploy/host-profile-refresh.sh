#!/usr/bin/env bash
# A19-P1-04 — daily host-profile refresh (systemd timer installed by deploy/host-prereqs.sh).
# Re-detects shared-web-host markers (cPanel/WHM, Plesk, DirectAdmin, httpd/exim/dovecot) and writes the
# signed deploy/state/host_profile.json the containers read on EVERY readiness check (read-only mount,
# no container restart needed). Older than 24 h / future-dated / unparseable ⇒ UNVERIFIED (blocks live).
set -u
cd "$(dirname "$0")/.." || exit 1
. deploy/lib.sh
. deploy/preflight.sh
markers=$(shared_web_host_markers)
profile=dedicated; [ -n "${markers}" ] && profile=shared-web-host
at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
key=$(cat secrets/ledger_anchor_key 2>/dev/null || true)
sig=""; [ -n "${key}" ] && { sig=$(host_profile_sig "${key}" "${profile}" "${markers}" "${at}") || sig=""; }
write_host_profile_file "${profile}" "${markers}" "${at}" "${sig}" || { echo "host-profile-refresh: could not write deploy/state/host_profile.json"; exit 1; }
echo "host-profile-refresh: ${profile}${markers:+ (${markers})} at ${at} $( [ -n "${sig}" ] && echo signed || echo UNSIGNED)"
