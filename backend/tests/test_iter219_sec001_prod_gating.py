"""SEC-001 (security audit, June 2026): the mock-broker router and the
REST demo broker partner seed must be gated through app_env.is_production
so the supported APP_ENV=prod shorthand disables them too — never a
literal "production" string comparison."""
import pytest


@pytest.mark.unit
class TestProdShorthandGating:
    def test_is_production_accepts_both_values(self, monkeypatch):
        from app_env import is_production
        for v in ("production", "prod", "PROD", " Production "):
            monkeypatch.setenv("APP_ENV", v)
            assert is_production() is True, v
        for v in ("preview", "dev", ""):
            monkeypatch.setenv("APP_ENV", v)
            assert is_production() is False, v

    def test_mockbroker_router_gated_by_is_production(self):
        import inspect
        src = inspect.getsource(__import__("server"))
        idx = src.index("mockbroker_router)")
        window = src[max(0, idx - 400):idx]
        assert "is_production" in window
        assert 'APP_ENV", "").lower() != "production"' not in src

    def test_demo_partner_seed_gated_by_is_production(self):
        import pathlib
        src = pathlib.Path(
            __import__("modules.pamm.models",
                       fromlist=["__file__"]).__file__).read_text()
        assert "ensure_rest_demo_partner" in src
        assert "is_production" in src
        assert '!= "production"' not in src
