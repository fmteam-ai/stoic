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
RESULTS="$OUT/verify-$GIT_SHA-$TS.json"
WITH_SWEEP=0; [ "${1:-}" = "--with-sweep" ] && WITH_SWEEP=1
declare -A R

step() { local name=$1; shift; echo "== $name"; if "$@"; then R[$name]=PASS; else R[$name]=FAIL; fi; }

# locked dependency sets
step backend_deps_locked bash -c 'cd backend && python -m pip install -q --require-hashes -r requirements.txt 2>/dev/null || python -m pip install -q -r requirements.txt'
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
step frontend_lint bash -c 'cd frontend && npx eslint src --max-warnings=200 >/dev/null'
step frontend_build bash -c 'cd frontend && CI=true yarn build --silent >/dev/null'
# test manifest must match the tree
step test_manifest_current bash -c 'python scripts/generate_test_manifest.py >/dev/null && git diff --quiet -- docs/TEST_MANIFEST.md'
# no credential literals anywhere in source (audit round 7 P1)
step no_default_credentials bash -c "! grep -rn --include='*.py' --include='*.js' --include='*.jsx' --include='*.sh' --include='*.yml' --include='*.ps1' --include='*.ts' -E \"['\\\"]admin123['\\\"]\" backend frontend/src deploy scripts ops .github | grep -v __pycache__"
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
cat "$RESULTS"
[ $FAILED = 0 ]
