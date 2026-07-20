#!/usr/bin/env bash
# Round 15 item 10 — CI job 2: INTEGRATION suite.
# Requires: pymongo/bson/motor installed, a reachable MongoDB (MONGO_URL/
# DB_NAME in backend/.env) and the backend running under supervisor.
# Covers routes, bridge heartbeat reconciliation and live-endpoint checks.
set -euo pipefail
cd "$(dirname "$0")/.."
python - <<'PY'
import asyncio
from seed import dependency_health_check
res = asyncio.run(dependency_health_check())
assert res["deps_ok"] and res["db_ok"] and res["indexes_ok"], res
print("dependency health:", res)
PY
exec python -m pytest tests/integration/ tests/test_*.py -q -p no:cacheprovider "$@"
