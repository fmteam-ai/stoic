#!/usr/bin/env bash
# One hermetic verification command (audit round 7 P2). From a clean checkout of the
# release tag it reproduces the claimed result WITHOUT version drift:
#   backend deps from the LOCKED requirements.txt, frontend deps from yarn.lock
#   (--frozen-lockfile), then: backend unit + integration tests, frontend lint +
#   build, test-manifest consistency, shell/python syntax, and (optionally, with a
#   signed non-production target) the mutating route-auth sweep. Emits an SBOM and a
#   signed results file under release/verification/.
#
#   scripts/verify_release.sh                 # hermetic: no live target needed
#   LIVE_TEST_BASE_URL=https://staging… ALLOW_MUTATING_AUTH_SWEEP=true AUTH_SWEEP_RUN_TOKEN=$(openssl rand -hex 16) \
#   TEST_ADMIN_EMAIL=… TEST_ADMIN_PASSWORD=… scripts/verify_release.sh --with-sweep
set -euo pipefail
cd "$(dirname "$0")/.."
GIT_SHA=$(git rev-parse HEAD 2>/dev/null || echo unknown)
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT=release/verification; mkdir -p "$OUT"
RESULTS="${VERIFY_RESULTS_PATH:-$OUT/verify-$GIT_SHA-$TS.json}"
WITH_SWEEP=0; [ "${1:-}" = "--with-sweep" ] && WITH_SWEEP=1
declare -A R

step() { local name=$1; shift; echo "== $name"; if "$@"; then R[$name]=PASS; else R[$name]=FAIL; fi; }

# locked dependency sets. requirements.txt is a pip freeze; the emergentintegrations
# internal wheel is installed WITHOUT deps from its exclusive index with a sha256 pin
# (dependency-confusion hardening — same recipe as .github/workflows/release.yml).
EI_SHA256=${EI_SHA256:-1822c409e24817689541891784b8ec3ff4dfed845ce8cd09fd4feb91ba3a4bdd}
step backend_deps_locked bash -c '
  set -e; grep -v "^emergentintegrations" backend/requirements.txt > /tmp/verify-reqs.txt
  python -m pip install -q -r /tmp/verify-reqs.txt --extra-index-url https://download.pytorch.org/whl/cpu
  if grep -q "^emergentintegrations" backend/requirements.txt && ! python -c "import emergentintegrations" 2>/dev/null; then
    for i in 1 2 3; do python -m pip download -q --no-deps emergentintegrations==0.2.0 -d /tmp/ei --index-url https://d33sy5i8bnduwe.cloudfront.net/simple/ && break; sleep 10; done
    echo "'"$EI_SHA256"'  /tmp/ei/emergentintegrations-0.2.0-py3-none-any.whl" | sha256sum -c -
    python -m pip install -q --no-deps /tmp/ei/emergentintegrations-0.2.0-py3-none-any.whl
  fi'
step frontend_deps_locked bash -c 'cd frontend && yarn install --frozen-lockfile --silent'
# SBOM (exact installed versions) — attached to the release evidence
step sbom bash -c "python -m pip freeze > $OUT/sbom-python-$GIT_SHA.txt && (cd frontend && yarn list --depth=0 --silent 2>/dev/null | sed 's/^[^a-zA-Z@]*//' > ../$OUT/sbom-node-$GIT_SHA.txt)"
# syntax / compile
step python_compile bash -c 'python -m compileall -q backend ops scripts >/dev/null'
step shell_syntax bash -c 'for f in deploy/*.sh scripts/*.sh deploy/drills/*.sh; do bash -n "$f" || exit 1; done'
# backend tests (CI-equivalent jobs, DB-free unit + Mongo integration)
step backend_unit bash -c 'cd backend && env -u MONGO_URL python -m pytest tests/unit -q -p no:cacheprovider'
step backend_integration bash -c 'cd backend && python -m pytest tests/integration -m integration -q -p no:cacheprovider'
# frontend lint + build
step frontend_lint bash -c 'cd frontend && npx eslint src >/dev/null'
step frontend_build bash -c 'cd frontend && CI=true yarn --silent build >/dev/null'
# test manifest must match the tree
step test_manifest_current bash -c 'cp docs/TEST_MANIFEST.md /tmp/manifest.before && python scripts/generate_test_manifest.py >/dev/null && cmp -s /tmp/manifest.before docs/TEST_MANIFEST.md; rc=$?; cp /tmp/manifest.before docs/TEST_MANIFEST.md; exit $rc'
# no credential literals anywhere in source (audit round 7 P1)
step credential_scan python3 scripts/secret_scan.py
# round 10 P1-10 — round-9/10 control tests are RELEASE GATES (Mongo-backed, no live server)
step round9_10_controls bash -c 'cd backend && env -u REACT_APP_BACKEND_URL -u LIVE_TEST_BASE_URL python -m pytest tests/unit/test_authority_matrix.py tests/unit/test_release_attestation.py tests/test_iter237_signer_round9.py::TestSignerConfigValidator tests/test_iter237_signer_round9.py::TestExternalSignerFailClosed -q -p no:cacheprovider'
step round10_hermetic_controls bash -c 'cd backend && LIVE_TEST_BASE_URL=http://hermetic.invalid TEST_ADMIN_EMAIL=hermetic@invalid TEST_ADMIN_PASSWORD=hermetic-not-a-credential-000 python -m pytest tests/test_iter239_round10.py -k "Scanner or Container or Turnstile or CanonicalDecision or FailsClosed" -q -p no:cacheprovider'
step keepalive_bounded bash -c 'grep -Eq "\"--timeout-keep-alive\", \"(([1-9][0-9]?)|([12][0-9][0-9])|300)\"" Dockerfile.backend'

if [ "$WITH_SWEEP" = 1 ]; then
  step route_auth_sweep bash -c 'cd backend && python -m pytest tests/test_iter228_route_auth_sweep.py -q -p no:cacheprovider'
fi

FAILED=0
{
  echo "{\"verification\":\"release\",\"build\":\"$GIT_SHA\",\"at\":\"$TS\",\"steps\":{"
  first=1
  for k in "${!R[@]}"; do
    [ $first = 1 ] || echo ","; first=0
    printf '  "%s": "%s"' "$k" "${R[$k]}"
    [ "${R[$k]}" = PASS ] || FAILED=1
  done
  echo "},\"result\":\"$([ $FAILED = 0 ] && echo PASS || echo FAIL)\"}"
} > "$RESULTS"
# sign the results with the dedicated key when present (CI provides it as a secret)
if [ -n "${LEDGER_ANCHOR_KEY:-}" ]; then
  SIG=$(python - "$RESULTS" <<'PY'
import hashlib, hmac, json, os, sys
body = json.load(open(sys.argv[1]))
payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
body["signature"] = hmac.new(os.environ["LEDGER_ANCHOR_KEY"].encode(), payload, hashlib.sha256).hexdigest()
body["evidence_hash"] = hashlib.sha256(payload).hexdigest()
json.dump(body, open(sys.argv[1], "w"), indent=1)
print(body["signature"][:16])
PY
)
  echo "signed results ($SIG…) → $RESULTS"
fi
if [ -z "${LEDGER_ANCHOR_KEY:-}" ] && [ "${REQUIRE_SIGNED_RESULTS:-0}" = 1 ]; then
  echo "!! REQUIRE_SIGNED_RESULTS=1 but LEDGER_ANCHOR_KEY is absent — unsigned verification is not release evidence"; FAILED=1
fi
cat "$RESULTS"
[ $FAILED = 0 ]
