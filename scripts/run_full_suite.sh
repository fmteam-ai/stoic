#!/usr/bin/env bash
# STOIC complete-suite gate — runs EVERY backend test class (unit,
# integration, http, and — when a live EA is attached — broker) against a
# running stack and writes a JUnit report. This is the staging/soak
# complement to the CI gates (which run the unit/truth/integration classes
# and the installer topology gate on every tag).
# Usage (on the staging/soak host, stack already running):
#   scripts/run_full_suite.sh [extra pytest args]
set -euo pipefail
cd "$(dirname "$0")/../backend"

STAMP=$(date -u +%Y%m%d-%H%M%S)
OUT="../test_reports/pytest/full_suite_${STAMP}.xml"
mkdir -p ../test_reports/pytest

echo "== STOIC full classified suite → ${OUT} =="
# Live-test safety gate (audit item 43 / r17 P0-02): the suite runs READ-ONLY
# by default — mutating live tests are skipped unless the CALLER exports
# STOIC_ALLOW_MUTATING_TESTS=YES explicitly (never defaulted here). The
# conftest still hard-refuses mutating tests when APP_ENV is production.
if [ "${STOIC_ALLOW_MUTATING_TESTS:-}" = "YES" ]; then
  echo "!! mutating live tests ENABLED by caller (STOIC_ALLOW_MUTATING_TESTS=YES)"
else
  export STOIC_ALLOW_MUTATING_TESTS=NO
  echo "   mutating live tests skipped (read-only run; export STOIC_ALLOW_MUTATING_TESTS=YES to opt in)"
fi
python -m pytest tests \
  -m "not broker and not external" \
  -q --junitxml="${OUT}" "$@"
echo "== full suite green — report: ${OUT} =="
echo "   broker-marked tests require a live EA: run with -m broker separately"
echo "   during the MT5 validation campaign (docs/MT5_VALIDATION_CAMPAIGN.md)."
