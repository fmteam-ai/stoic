#!/usr/bin/env bash
# STOIC reproducible unit lane — one command, zero tribal knowledge.
#
#   ./scripts/test_unit.sh          (or: make test-unit)
#
# Validates the interpreter + every dependency the unit lane needs
# (including bson/pymongo, pulled in transitively by backend modules even
# though unit tests never touch a database), installs anything missing from
# requirements.txt, asserts the collected tests match docs/TEST_MANIFEST.md
# (per file, parametrized cases collapsed — the generator's unit of count),
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

# Manifest truth check — the lane must collect EXACTLY the tests published in
# docs/TEST_MANIFEST.md (CI regenerates that file and fails on drift).
# The manifest counts test FUNCTIONS per file (static `def test_*`); pytest
# collects one item per parametrized case. Compare like with like: collapse
# parametrized ids (`test_x[a]`, `test_x[b]` -> `test_x`) and compare PER FILE,
# so a file that silently fails to collect is caught too.
COLLECT_OUT=$($PY -m pytest tests/unit --collect-only -q 2>/dev/null || true)
CMP=$(COLLECT_OUT="$COLLECT_OUT" $PY - <<'PYEOF'
import collections, os, re, sys
out = os.environ["COLLECT_OUT"].splitlines()
items = [l.strip() for l in out if "::" in l]
hits = [re.match(r"^(\d+) tests? collected", l) for l in out]
total = next((int(h.group(1)) for h in hits if h), None)
if total is None or not items:
    print("FATAL collection-failed")
    sys.exit(0)
funcs = collections.Counter()
for base in {re.sub(r"\[.*\]$", "", i) for i in items}:
    funcs["backend/" + base.split("::")[0]] += 1
man = {}
try:
    for line in open("../docs/TEST_MANIFEST.md"):
        r = re.match(r"^\| `(backend/tests/unit/[^`]+)` \| unit \| (\d+) \|$", line)
        if r:
            man[r.group(1)] = int(r.group(2))
except OSError:
    pass
if not man:
    print(f"OK {total} {sum(funcs.values())} n/a")
    sys.exit(0)
diff = [f"{f}: collected {funcs.get(f, 0)} manifest {man.get(f, 0)}"
        for f in sorted(set(man) | set(funcs)) if funcs.get(f, 0) != man.get(f, 0)]
print(("DRIFT " + " | ".join(diff[:20])) if diff
      else f"OK {total} {sum(funcs.values())} {sum(man.values())}")
PYEOF
)
case "$CMP" in
  "FATAL "*)
    echo "FATAL: unit-lane collection failed — run" >&2
    echo "       $PY -m pytest tests/unit --collect-only -q   for details" >&2
    exit 2 ;;
  "DRIFT "*)
    echo "FATAL: collected unit tests differ from docs/TEST_MANIFEST.md (per file," >&2
    echo "       parametrized cases collapsed): ${CMP#DRIFT }" >&2
    echo "       Regenerate the manifest: python scripts/generate_test_manifest.py" >&2
    exit 2 ;;
esac
read -r _ COLLECTED FUNCS EXPECTED <<< "$CMP"

echo "== unit lane: ${COLLECTED} test items / ${FUNCS} test functions (manifest: ${EXPECTED:-n/a}) =="
exec $PY -m pytest tests/unit -q "$@"
