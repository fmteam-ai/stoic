"""Shared live-stack test target resolution (review P1).

Every HTTP suite that talks to a running deployment must resolve its
target through here — never crash collection when the env is absent.
Order: REACT_APP_BACKEND_URL → LIVE_TEST_BASE_URL → frontend/.env.
"""
import os


def get_base_url() -> str:
    url = (os.environ.get("REACT_APP_BACKEND_URL")
           or os.environ.get("LIVE_TEST_BASE_URL") or "")
    if not url:
        env = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "frontend", ".env")
        if os.path.exists(env):
            with open(env) as f:
                for line in f:
                    if line.startswith("REACT_APP_BACKEND_URL="):
                        url = line.split("=", 1)[1].strip()
    return url.rstrip("/")


def require_live_base_url() -> str:
    """Module-level guard: skip the whole suite when no live target exists
    (e.g. CI unit/integration jobs) instead of failing collection."""
    import pytest
    url = get_base_url()
    if not url:
        pytest.skip("no live test target — set REACT_APP_BACKEND_URL or "
                    "LIVE_TEST_BASE_URL", allow_module_level=True)
    return url
