"""Shared live-stack test target resolution (review P1) and test-admin
credentials (audit round 7 P1: NO credential literals in source).

Every HTTP suite that talks to a running deployment must resolve its
target through here — never crash collection when the env is absent.
Order: REACT_APP_BACKEND_URL → LIVE_TEST_BASE_URL → frontend/.env.

Admin credentials come ONLY from the environment (TEST_ADMIN_EMAIL /
TEST_ADMIN_PASSWORD, falling back to ADMIN_EMAIL / ADMIN_PASSWORD which CI
generates per run). Known default passwords are refused before any network
call, so an embedded weak pair can never "accidentally work" anywhere.
"""
import hashlib
import os

# sha256 of well-known weak defaults — the literals themselves never appear in source
KNOWN_DEFAULT_PASSWORD_HASHES = {
    "240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9",   # the historical seed default
    "8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92",   # 123456
    "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8",   # password
}


def _sha(v: str) -> str:
    return hashlib.sha256((v or "").encode()).hexdigest()


def is_known_default_password(pw: str) -> bool:
    return _sha(pw) in KNOWN_DEFAULT_PASSWORD_HASHES


def _env_admin() -> tuple:
    return (os.environ.get("TEST_ADMIN_EMAIL") or os.environ.get("ADMIN_EMAIL") or "",
            os.environ.get("TEST_ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD") or "")


# module constants for the HTTP suites: "" when unset (conftest fails live runs fast)
ADMIN_EMAIL, ADMIN_PASSWORD = _env_admin()


def admin_credentials(strict: bool = True) -> tuple:
    """(email, password) from the environment. strict → RuntimeError BEFORE
    any network call when missing or when the password is a known default."""
    em, pw = _env_admin()
    problems = []
    if not em or not pw:
        problems.append("TEST_ADMIN_EMAIL/TEST_ADMIN_PASSWORD (or ADMIN_EMAIL/ADMIN_PASSWORD) not set")
    if pw and is_known_default_password(pw):
        problems.append("admin password is a KNOWN DEFAULT — rotate it; defaults are refused")
    if problems and strict:
        raise RuntimeError("test admin credentials refused: " + "; ".join(problems))
    return em, pw


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


_ADMIN_CREDS = None


def resolve_admin_credentials() -> tuple:
    """(email, password) that authenticates against the live target —
    environment-provided only (see admin_credentials)."""
    global _ADMIN_CREDS
    if _ADMIN_CREDS:
        return _ADMIN_CREDS
    _ADMIN_CREDS = admin_credentials(strict=False)
    return _ADMIN_CREDS
