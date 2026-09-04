#!/usr/bin/env bash
# STOIC reproducible unit lane — one command, zero tribal knowledge.
#
#   ./scripts/test_unit.sh          (or: make test-unit)
#
# Validates the interpreter + every dependency the unit lane needs
# (including bson/pymongo, pulled in transitively by backend modules even
# though unit tests never touch a database), installs anything missing from
# requirements.txt, asserts the collected count matches docs/TEST_MANIFEST.md,
# then runs the exact unit lane.
set -euo pipefail
cd "$(dirname "$0")/../backend"

PY=${PYTHON:-python3}

REQUIRED_PY="3.11"
HAVE_PY=$($PY -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
if [ "$HAVE_PY" != "$REQUIRED_PY" ]; then
    echo "FATAL: Python ${REQUIRED_PY}.x required, found ${HAVE_PY}." >&2
    echo "       Point PYTHON=<path-to-python3.11> or use pyenv/venv." >&2
    exit 2
fi

# Dependency doctor — checks every import the lane needs BEFORE pytest
# collection, so a fresh checkout never dies with cryptic collection errors.
MISSING=$($PY - <<'EOF'
import importlib.util
mods = ["pytest", "dotenv", "bson", "pymongo", "motor", "fastapi",
        "pydantic", "cryptography", "requests", "jwt", "bcrypt"]
print(" ".join(m for m in mods if importlib.util.find_spec(m) is None))
EOF
)
if [ -n "${MISSING// /}" ]; then
    echo "== missing modules: ${MISSING} — installing requirements.txt =="
    $PY -m pip install -q -r requirements.txt
fi

# Manifest truth check — the lane must collect EXACTLY the count published
# in docs/TEST_MANIFEST.md (CI regenerates that file and fails on drift).
EXPECTED=$(grep -oP '^\| unit \|.*\| \K[0-9]+(?= \|$)' ../docs/TEST_MANIFEST.md | head -1)
COLLECTED=$($PY -m pytest tests/unit --collect-only -q 2>/dev/null \
            | grep -oP '^[0-9]+(?= tests? collected)' | tail -1)
if [ -z "${COLLECTED:-}" ]; then
    echo "FATAL: unit-lane collection failed — run" >&2
    echo "       $PY -m pytest tests/unit --collect-only -q   for details" >&2
    exit 2
fi
if [ -n "${EXPECTED:-}" ] && [ "$COLLECTED" != "$EXPECTED" ]; then
    echo "FATAL: collected ${COLLECTED} unit tests but docs/TEST_MANIFEST.md" >&2
    echo "       declares ${EXPECTED}. Regenerate the manifest:" >&2
    echo "       python scripts/generate_test_manifest.py" >&2
    exit 2
fi

echo "== unit lane: ${COLLECTED} tests (manifest: ${EXPECTED:-n/a}) =="
exec $PY -m pytest tests/unit -q "$@"
