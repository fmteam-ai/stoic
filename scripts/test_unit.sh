#!/usr/bin/env bash
# STOIC reproducible unit lane — one command, zero tribal knowledge.
#
#   ./scripts/test_unit.sh          (or: make test-unit)
#
# Validates the interpreter + every dependency the unit lane needs
# (including bson/pymongo, pulled in transitively by backend modules even
# though unit tests never touch a database), installs anything missing from
# requirements.txt, verifies docs/TEST_MANIFEST.md is current (same check as CI),
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

# Manifest truth check (M114-5) — the SAME check CI runs: docs/TEST_MANIFEST.md counts declared
# `def test_` functions per file, while pytest collects parametrised cases, so the two totals legitimately
# differ (996 declared vs 1160 collected). Never compare them — regenerate-and-diff instead.
$PY ../scripts/generate_test_manifest.py --check || {
    echo "FATAL: docs/TEST_MANIFEST.md drifted from the tree — regenerate: python scripts/generate_test_manifest.py" >&2
    exit 2
}
COLLECTED=$($PY -m pytest tests/unit --collect-only -q 2>/dev/null \
            | grep -oP '^[0-9]+(?= tests? collected)' | tail -1)
if [ -z "${COLLECTED:-}" ]; then
    echo "FATAL: unit-lane collection failed — run" >&2
    echo "       $PY -m pytest tests/unit --collect-only -q   for details" >&2
    exit 2
fi
EXPECTED=$(grep -oP '^\| unit \|.*\| \K[0-9]+(?= \|$)' ../docs/TEST_MANIFEST.md | head -1)

echo "== unit lane: ${COLLECTED} collected cases (${EXPECTED:-n/a} declared test functions in docs/TEST_MANIFEST.md) =="
exec $PY -m pytest tests/unit -q "$@"
