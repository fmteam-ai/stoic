"""Integration-suite conftest — scratch-DB isolation.

Several integration tests point DB_NAME at a throwaway database via
os.environ. Without restoration this leaks into OTHER suites in the same
pytest process (HTTP tests then flip flags in the wrong DB). Snapshot and
restore around every test, and reset the cached Motor client both ways."""
import os

import pytest


@pytest.fixture(autouse=True)
def _restore_db_name():
    orig = os.environ.get("DB_NAME")
    yield
    if orig is None:
        os.environ.pop("DB_NAME", None)
    else:
        os.environ["DB_NAME"] = orig
    try:
        import database
        database._client = None
    except Exception:  # noqa: BLE001
        pass
