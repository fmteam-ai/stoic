"""The bridge-rotation password challenge honours the same server-side test
bypass as step-up — and never in production."""
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _req(hdr):
    return SimpleNamespace(headers={"X-Step-Up-Bypass": hdr} if hdr is not None else {})


def test_bypass_accepted_outside_production(monkeypatch):
    from routes.account_routes import _test_bypass_ok
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STEP_UP_BYPASS_TOKEN", "t" * 32)
    assert _test_bypass_ok(_req("t" * 32)) is True
    assert _test_bypass_ok(_req("wrong")) is False
    assert _test_bypass_ok(_req(None)) is False


def test_bypass_refused_in_production(monkeypatch):
    from routes.account_routes import _test_bypass_ok
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("STEP_UP_BYPASS_TOKEN", "t" * 32)
    assert _test_bypass_ok(_req("t" * 32)) is False
