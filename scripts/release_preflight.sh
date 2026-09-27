#!/usr/bin/env bash
# Local preflight before tagging a release (docs/RELEASE_RUNBOOK.md §1). Read-only: fails on the first drift.
set -euo pipefail
cd "$(dirname "$0")/.."
ok=0; fail=0
step() { local name="$1"; shift; if "$@" >/tmp/preflight.log 2>&1; then echo "OK    ${name}"; ok=$((ok+1)); else echo "FAIL  ${name}"; tail -n 5 /tmp/preflight.log; fail=$((fail+1)); fi; }
step test_manifest_current   python3 scripts/generate_test_manifest.py --check
step release_summary_current python3 scripts/generate_release_summary.py --check
step provenance_consistent   python3 scripts/release_consistency_check.py
step secret_scan_clean       python3 scripts/secret_scan.py
step model_manifest_verified bash -c 'cd backend && set -a && . <(grep -E "^(ED25519_SIGNING_KEY_B64|RELEASE_PUBLIC_KEY_B64)=" .env 2>/dev/null || true) && set +a && python -m model_manifest verify'
echo "preflight: ${ok} ok, ${fail} failed"
[ "${fail}" = 0 ]
