"""review item 1 — unit tests must be genuinely independent: no live
MongoDB, motor clients, or environment DB URLs anywhere under tests/unit."""
import pathlib

import pytest

pytestmark = pytest.mark.unit

FORBIDDEN = ("AsyncIOMotorClient", "MongoClient", 'environ["MONGO_URL"]',
             "environ['MONGO_URL']", "environ.get(\"MONGO_URL\"",
             "environ.get('MONGO_URL'")
UNIT_DIR = pathlib.Path(__file__).parent


def test_no_live_db_usage_in_unit_tests():
    offenders = []
    for path in UNIT_DIR.rglob("*.py"):
        if path.name == pathlib.Path(__file__).name:
            continue
        src = path.read_text()
        for token in FORBIDDEN:
            if token in src:
                offenders.append(f"{path.relative_to(UNIT_DIR)}: {token}")
    assert not offenders, (
        "unit tests must not touch a live database — move these to "
        f"tests/integration/: {offenders}")
