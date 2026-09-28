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

# Legacy HMAC attestation tests need SOME shared secret in CI.
if not os.environ.get("JWT_SECRET") \
        and not os.environ.get("PERF_SIGNING_KEY"):
    import secrets as _secrets
    os.environ["JWT_SECRET"] = _secrets.token_hex(32)


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



def run_async(coro):
    """Delegate to the suite-wide shared loop owned by tests/conftest.py so
    motor singletons stay bound to ONE loop across unit/integration suites."""
    import sys
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", "") or ""
        if f.endswith(os.path.join("tests", "conftest.py")) and hasattr(m, "run_async"):
            return m.run_async(coro)
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _synthetic_nl_effect_posture(monkeypatch):
    """r18 P1-01: fenced NL effects refuse the non-atomic standalone fallback whenever
    capital can be touched. The integration suite shares the preview database (which
    may hold a live-enabled account) and CI runs on a replica set, so the suite
    declares the SYNTHETIC posture explicitly; the real predicate is covered by
    test_r18_audit.py::test_transactions_required_by_capability."""
    import nl_execution as nx
    if not hasattr(nx, "_transactions_required_real"):
        nx._transactions_required_real = nx.transactions_required

    async def _synthetic(db):
        return False
    monkeypatch.setattr(nx, "transactions_required", _synthetic)
