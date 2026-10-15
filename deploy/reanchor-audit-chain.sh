#!/usr/bin/env bash
# v1.60.43 — run the audit-chain re-anchor INSIDE the backend image (its Python + libs; the host runs 3.9).
#   deploy/reanchor-audit-chain.sh                                   # verify + dry-run
#   deploy/reanchor-audit-chain.sh --apply --actor ops@… --reason "…" # apply
# The script is shipped in the image at /app/scripts/reanchor_audit_chain.py (Dockerfile.backend); the fallback
# pipes the checked-out copy into the running container for images built before v1.60.43.
set -euo pipefail
cd "$(dirname "$0")/.."
if docker compose exec -T backend test -f /app/scripts/reanchor_audit_chain.py 2>/dev/null; then
  exec docker compose exec -T backend python /app/scripts/reanchor_audit_chain.py "$@"
fi
echo "   (image predates v1.60.43 — piping scripts/reanchor_audit_chain.py into the backend container)"
exec docker compose exec -T backend python - "$@" < scripts/reanchor_audit_chain.py
