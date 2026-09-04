"""Unit tests — audit items 43/44: test-identity blocklist and the
live-test safety gate logic (pure logic; no DB, no network)."""
import importlib
import os
from unittest.mock import patch

import test_identity


def _check(email):
    return test_identity.is_disposable_test_identity(email)


def test_default_patterns_block_disposable_identities():
    assert _check("qa_user@example.com")
    assert _check("someone@example.org")
    assert _check("bot@test.local")
    assert _check("TEST_runner@stoicaibot.com")
    assert _check("nonadmin_ab12cd34@example.com")
    assert _check("trader+test@gmail.com")
    assert _check("someone@example-a1b2c3.com")


def test_real_identities_are_never_blocked():
    assert not _check("admin@stoicaibot.com")
    assert not _check("jane.doe@gmail.com")
    assert not _check("attestor@brokerage.co.uk")
    assert not _check("")
    assert not _check(None)


def test_patterns_configurable_via_env():
    with patch.dict(os.environ,
                    {"TEST_IDENTITY_BLOCK_PATTERNS": r"@blocked\.io$"}):
        assert _check("x@blocked.io")
        # default patterns are REPLACED, not appended
        assert not _check("x@example.com")


def test_production_gate_wiring():
    """login guard = is_production() AND is_disposable_test_identity()."""
    import app_env
    with patch.dict(os.environ, {"APP_ENV": "production"}):
        importlib.reload(app_env)
        assert app_env.is_production()
        assert app_env.is_production() and _check("t@example.com")
        assert not (app_env.is_production() and _check("real@user.com"))
    with patch.dict(os.environ, {"APP_ENV": "preview"}):
        importlib.reload(app_env)
        assert not app_env.is_production()
