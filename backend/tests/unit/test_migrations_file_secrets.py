"""Migrations run via `docker compose exec backend python -m migrations.X`
must resolve Docker-secret files (X_FILE → X) at import, like the app does;
otherwise MONGO_URL is empty on secret-file deployments and pymongo fails
with "Empty host". A neutral variable proves the resolver ran."""
import importlib
import os
import sys

import pytest

pytestmark = pytest.mark.unit
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


@pytest.mark.parametrize("module", ["migrations.hash_bridge_tokens", "migrations.rekey_vault"])
def test_migration_resolves_secret_files_on_import(module, tmp_path, monkeypatch):
    secret = tmp_path / "probe"
    secret.write_text("from-secret-file\n")
    monkeypatch.setenv("STOIC_MIGRATION_PROBE", "")
    monkeypatch.setenv("STOIC_MIGRATION_PROBE_FILE", str(secret))
    sys.modules.pop(module, None)
    importlib.import_module(module)
    assert os.environ.get("STOIC_MIGRATION_PROBE") == "from-secret-file"
