"""Integration-suite conftest — scratch-DB isolation.

Several integration tests point DB_NAME at a throwaway database via
os.environ. Without restoration this leaks into OTHER suites in the same
pytest process (HTTP tests then flip flags in the wrong DB). Snapshot and
restore around every test, and reset the cached Motor client both ways."""
import base64
import os

import pytest

# CI has no signing key configured — attestation tests exercise the signing
# path itself, so provide an EPHEMERAL Ed25519 key (never overrides a real
# key; preview/production set ED25519_SIGNING_KEY_B64 in the environment).
if not os.environ.get("ED25519_SIGNING_KEY_B64"):
    from cryptography.hazmat.primitives import serialization as _ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey as _EphemeralKey,
    )
    os.environ["ED25519_SIGNING_KEY_B64"] = base64.b64encode(
        _EphemeralKey.generate().private_bytes(
            _ser.Encoding.Raw, _ser.PrivateFormat.Raw,
            _ser.NoEncryption())).decode()


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
