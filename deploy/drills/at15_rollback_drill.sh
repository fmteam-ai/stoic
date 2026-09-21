#!/usr/bin/env bash
# AT-15 — signed release + forced-failure rollback drill (run on STAGING, in the checkout).
#
#   deploy/drills/at15_rollback_drill.sh --yes            # causes a few minutes of downtime
#
# 1. verifies the release evidence for the CURRENT checkout: signed attestation
#    (commit SHA, tests, scans, gates, image digests, EA compile flag) and, in
#    registry mode, the cosign signatures of the pulled digests;
# 2. records the running truth (SHA, image digest, schema version, containers);
# 3. forces the release-readiness probe to FAIL (STOIC_DRILL_FORCE_READINESS_FAIL=1)
#    and runs deploy/update.sh — which MUST auto-rollback;
# 4. removes the fault and proves the previous signed digest/SHA is back, the
#    stack is READY again and the schema version did not move.
# Evidence: release/drills/at15-<ts>.json. Exit 0 = PASS.
set -uo pipefail
cd "$(dirname "$0")/../.."
. deploy/lib.sh

[ "${1:-}" = "--yes" ] || { echo "AT-15 causes downtime on this host. Re-run with --yes (staging only)."; exit 2; }
TS=$(date -u +%Y%m%dT%H%M%SZ); mkdir -p release/drills
EV=release/drills/at15-${TS}.json
PASS=1; NOTES=()
note() { NOTES+=("$1"); echo "   $1"; }
fail() { PASS=0; NOTES+=("FAIL: $1"); echo "!! $1"; }
jget() { python3 -c 'import json,sys;d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d=d.get(k) if isinstance(d,dict) else None
print("" if d is None else d)' "$1"; }

echo "== AT-15 · 1/4 release evidence for $(git rev-parse --short HEAD)"
resolve_git_sha
TAG=$(git tag --points-at HEAD | grep -E '^v[0-9]' | head -1)
if [ -f release/attestation.current.json ]; then
  if python3 scripts/release_attestation.py verify --file release/attestation.current.json --sha "${GIT_SHA}" ${TAG:+--tag "$TAG"} \
       $( [ "$(deploy_mode)" = registry ] && echo --require-images ); then
    note "attestation verified for ${GIT_SHA} (tests/scans/gates/EA gate inside the record)"
  else fail "attestation does not verify for the running commit"; fi
  ATT_TESTS=$(jget tests.tests < release/attestation.current.json)
  ATT_DECISION=$(jget promotion_decision < release/attestation.current.json)
  note "attested tests=${ATT_TESTS} decision=${ATT_DECISION}"
  [ -f release/attestation.current.bundle ] && note "Rekor bundle present (offline verification material)"
else
  fail "release/attestation.current.json missing — this host was never deployed through the attestation gate"
fi
if [ "$(deploy_mode)" = registry ]; then
  repo=$(grep -E '^GITHUB_REPO=' .env | cut -d= -f2-); [ -n "$repo" ] || repo=$(_repo_slug)
  for d in "$(grep -E '^STOIC_BACKEND_IMAGE=' .env | cut -d= -f2-)" "$(grep -E '^STOIC_FRONTEND_IMAGE=' .env | cut -d= -f2-)"; do
    [ -n "$d" ] || { fail "pinned image missing in .env"; continue; }
    if ensure_cosign && cosign verify "$d" --certificate-identity "https://github.com/${repo}/.github/workflows/release.yml@refs/tags/${TAG}" \
         --certificate-github-workflow-repository "$repo" --certificate-oidc-issuer https://token.actions.githubusercontent.com >/dev/null 2>&1; then
      note "image signature OK: $d"
    else fail "image signature invalid: $d"; fi
    [ "$(docker inspect --format '{{index .RepoDigests 0}}' "$d" 2>/dev/null)" = "$d" ] && note "pulled digest == attested: ${d#*@}" || fail "pulled digest != attested for $d"
  done
fi

echo "== AT-15 · 2/4 running truth before the fault"
PRE_SHA=$(grep -E '^GIT_SHA=' .env | cut -d= -f2-); PRE_DIGEST=$(grep -E '^STOIC_IMAGE_DIGEST=' .env | cut -d= -f2-)
PRE_HEAD=$(git rev-parse HEAD)
PRE_BODY=$(wait_release_ready 20) || { fail "stack is not READY before the drill — fix that first"; }
PRE_SCHEMA=$(printf '%s' "$PRE_BODY" | jget checks.schema.detail 2>/dev/null || true)
PRE_CONTAINERS=$(docker compose ps --status running --format '{{.Service}}' | sort | tr '\n' ',')
note "pre: head=${PRE_HEAD:0:12} digest=${PRE_DIGEST:0:19} containers=${PRE_CONTAINERS}"

echo "== AT-15 · 3/4 forcing readiness failure and running deploy/update.sh (expect auto-rollback)"
set_kv backend/.env STOIC_DRILL_FORCE_READINESS_FAIL 1
ROLLED=0
if REF="$(git rev-parse HEAD)" deploy/update.sh >release/drills/at15-${TS}.update.log 2>&1; then
  fail "update.sh reported SUCCESS although readiness was forced to fail — rollback path NOT exercised"
else
  grep -q "auto-rollback-from" release/drills/at15-${TS}.update.log && ROLLED=1
  [ "$ROLLED" = 1 ] && note "update.sh auto-rolled back (releases.log entry written)" || fail "update.sh failed without performing the rollback"
fi

echo "== AT-15 · 4/4 removing the fault, verifying the restored release"
set_kv backend/.env STOIC_DRILL_FORCE_READINESS_FAIL 0
docker compose up -d backend >/dev/null 2>&1
POST_BODY=$(wait_release_ready 45) || fail "stack did not return to READY after the rollback"
POST_SHA=$(grep -E '^GIT_SHA=' .env | cut -d= -f2-); POST_DIGEST=$(grep -E '^STOIC_IMAGE_DIGEST=' .env | cut -d= -f2-)
POST_HEAD=$(git rev-parse HEAD)
POST_SCHEMA=$(printf '%s' "$POST_BODY" | jget checks.schema.detail 2>/dev/null || true)
POST_CONTAINERS=$(docker compose ps --status running --format '{{.Service}}' | sort | tr '\n' ',')
[ "$POST_HEAD" = "$PRE_HEAD" ] && note "checkout restored to ${PRE_HEAD:0:12}" || fail "checkout moved: ${PRE_HEAD:0:12} → ${POST_HEAD:0:12}"
[ "$POST_SHA" = "$PRE_SHA" ] && note "GIT_SHA pin restored" || fail "GIT_SHA pin changed"
[ "$(deploy_mode)" != registry ] || { [ "$POST_DIGEST" = "$PRE_DIGEST" ] && note "signed image digest restored" || fail "image digest changed after rollback"; }
[ "$POST_SCHEMA" = "$PRE_SCHEMA" ] && note "schema version unchanged (${POST_SCHEMA:-n/a})" || fail "schema check changed: '${PRE_SCHEMA}' → '${POST_SCHEMA}'"
[ "$POST_CONTAINERS" = "$PRE_CONTAINERS" ] && note "same containers running" || fail "container set differs: ${PRE_CONTAINERS} vs ${POST_CONTAINERS}"
verify_running_sha >/dev/null 2>&1 && note "running build SHA == checkout" || fail "running build SHA != checkout"

python3 - "$EV" "$PASS" "$PRE_HEAD" "$POST_HEAD" "$PRE_DIGEST" "$POST_DIGEST" "$ROLLED" "${NOTES[@]}" <<'EOF'
import json, sys, datetime
ev, ok, preh, posth, pred, postd, rolled, *notes = sys.argv[1:]
json.dump({"drill": "AT-15", "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "result": "PASS" if ok == "1" else "FAIL", "pre_head": preh, "post_head": posth,
           "pre_digest": pred, "post_digest": postd, "auto_rollback_observed": rolled == "1",
           "notes": notes}, open(ev, "w"), indent=1)
EOF
echo "AT-15 $([ "$PASS" = 1 ] && echo PASS || echo FAIL) — evidence ${EV}"
[ "$PASS" = 1 ]
