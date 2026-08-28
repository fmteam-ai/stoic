"""review item 1 + v62.6 — unit tests must be PHYSICALLY independent:
no live MongoDB, motor clients, or environment DB URLs in tests/unit,
and any file-level unit-marked module anywhere under tests/."""
import pathlib

import pytest

pytestmark = pytest.mark.unit

FORBIDDEN = ("AsyncIOMotorClient", "MongoClient", 'environ["MONGO_URL"]',
             "environ['MONGO_URL']", "environ.get(\"MONGO_URL\"",
             "environ.get('MONGO_URL'")
UNIT_DIR = pathlib.Path(__file__).parent
TESTS_DIR = UNIT_DIR.parent
SELF = pathlib.Path(__file__).name


def _offenders(paths):
    out = []
    for path in paths:
        if path.name == SELF:
            continue
        src = path.read_text()
        for token in FORBIDDEN:
            if token in src:
                out.append(f"{path.relative_to(TESTS_DIR)}: {token}")
    return out


def test_no_live_db_usage_in_unit_tests():
    offenders = _offenders(UNIT_DIR.rglob("*.py"))
    assert not offenders, (
        "unit tests must not touch a live database — move these to "
        f"tests/integration/: {offenders}")


def test_no_live_db_in_unit_marked_modules_anywhere():
    """Physical isolation is not a directory convention: ANY module that
    declares itself unit at file level must be DB-free wherever it lives."""
    unit_marked = [
        p for p in TESTS_DIR.rglob("test_*.py")
        if UNIT_DIR not in p.parents
        and "pytestmark = pytest.mark.unit" in p.read_text()]
    offenders = _offenders(unit_marked)
    assert not offenders, (
        "file-level unit-marked modules must not touch a live database — "
        f"re-mark as integration or remove DB usage: {offenders}")
