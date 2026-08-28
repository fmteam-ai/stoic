"""One-off: apply module-level pytest markers to unmarked test files."""
import re
import sys
from pathlib import Path

TESTS = Path("/app/backend/tests")


def classify(src: str) -> str:
    if re.search(r"REACT_APP_BACKEND_URL|BACKEND_URL|requests\.(get|post|put|patch|delete)|httpx", src):
        return "http"
    if re.search(r"MONGO_URL|AsyncIOMotorClient|motor\b|get_db\(", src):
        return "integration"
    if re.search(r"MetaTrader5|mt5\.", src):
        return "broker"
    return "unit"


def main():
    changed = skipped = 0
    for f in sorted(TESTS.glob("test_*.py")):
        src = f.read_text()
        if re.search(r"pytest\.mark\.(unit|integration|http|broker|external|chaos|soak)\b", src) or "pytestmark" in src:
            skipped += 1
            continue
        marker = classify(src)
        src += (f"\n\nimport pytest as _pytest  # noqa: E402\n"
                f"pytestmark = _pytest.mark.{marker}\n")
        f.write_text(src)
        changed += 1
    print(f"marked {changed}, skipped(already marked) {skipped}")


if __name__ == "__main__":
    sys.exit(main())
