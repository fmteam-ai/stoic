#!/usr/bin/env bash
# Pin every base image (Dockerfile FROM lines + compose `image:` lines) to an
# immutable registry digest:  FROM node:22-alpine  →  FROM node:22-alpine@sha256:…
#
#   scripts/pin_image_digests.sh            # resolve + rewrite in place
#   scripts/pin_image_digests.sh --check    # exit 1 if any image is unpinned (CI)
#   scripts/pin_image_digests.sh --refresh  # re-resolve already-pinned tags (base image updates)
#
# Digest resolution order: `docker buildx imagetools inspect` → `crane digest`
# → Docker Hub registry API via curl (anonymous token). The tag is kept next to
# the digest so humans (and Dependabot/Renovate) still see the version; Docker
# uses the digest. Re-run with --refresh to pick up patched base images.
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="pin"
case "${1:-}" in --check) MODE="check" ;; --refresh) MODE="refresh" ;; "") ;; *) echo "usage: $0 [--check|--refresh]"; exit 2 ;; esac

FILES=(Dockerfile.backend Dockerfile.frontend Dockerfile.migrator deploy/signer/Dockerfile
       docker-compose.yml docker-compose.tls.yml)

resolve() {  # resolve <image:tag> → sha256:… (multi-arch index digest)
  local ref="$1" d=""
  if command -v docker >/dev/null 2>&1; then
    d=$(docker buildx imagetools inspect "$ref" --format '{{json .Manifest}}' 2>/dev/null \
        | python3 -c 'import sys,json;print(json.load(sys.stdin)["digest"])' 2>/dev/null || true)
  fi
  if [ -z "$d" ] && command -v crane >/dev/null 2>&1; then d=$(crane digest "$ref" 2>/dev/null || true); fi
  if [ -z "$d" ]; then
    local name="${ref%%:*}" tag="${ref#*:}" tok
    [[ "$name" == */* ]] || name="library/${name}"
    tok=$(curl -fsS -m 20 "https://auth.docker.io/token?service=registry.docker.io&scope=repository:${name}:pull" \
          | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])' 2>/dev/null || true)
    [ -n "$tok" ] && d=$(curl -fsSI -m 20 -H "Authorization: Bearer ${tok}" \
        -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json" \
        "https://registry-1.docker.io/v2/${name}/manifests/${tag}" 2>/dev/null \
        | tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2}' || true)
  fi
  [[ "$d" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "ERROR: cannot resolve digest for ${ref}" >&2; return 1; }
  printf '%s' "$d"
}

unpinned=0
for f in "${FILES[@]}"; do
  [ -f "$f" ] || continue
  # image refs: `FROM ref [AS x]` and compose `image: ref` (skip ${VAR} refs)
  while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    base="${ref%%@*}"
    if [[ "$ref" == *@sha256:* ]] && [ "$MODE" != "refresh" ]; then continue; fi
    if [ "$MODE" = "check" ]; then echo "UNPINNED: ${f}: ${ref}"; unpinned=1; continue; fi
    dig=$(resolve "$base") || exit 1
    python3 - "$f" "$ref" "${base}@${dig}" <<'PY'
import re, sys
path, old, new = sys.argv[1:]
src = open(path).read()
pat = re.compile(r'(^\s*(?:FROM\s+|image:\s*))' + re.escape(old) + r'(?=\s|$)', re.M)
src2 = pat.sub(lambda m: m.group(1) + new, src)
open(path, "w").write(src2)
PY
    echo "pinned ${f}: ${base} → ${dig}"
  done < <(grep -E '^\s*(FROM\s+|image:\s*)[^$]' "$f" | sed -E 's/^\s*(FROM\s+|image:\s*)//; s/\s+AS\s+.*$//I; s/\s+#.*$//' | sort -u)
done

if [ "$MODE" = "check" ] && [ "$unpinned" = 1 ]; then
  echo "base images are not digest-pinned — run scripts/pin_image_digests.sh (needs registry access)"; exit 1
fi
exit 0
