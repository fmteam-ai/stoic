"""Global test fixtures.

CRITICAL: This file MUST prevent any test from emitting a real Telegram
push, email, or other outbound side-effect.

Background: several tests reuse the live admin account (admin@trading.bot)
which has a real Telegram bot_token + chat_id configured. Without this
guard, every backend test run was sending fake "Trade Opened" messages to
the user's Telegram group (reported by user 2026-06-23).

The autouse `_silence_outbound_notifications` fixture monkey-patches the
notifier's HTTP send to a no-op for the entire test session.
"""
import os
import pytest

# iter-143 · Load backend/.env (MONGO_URL, DB_NAME) and default
# REACT_APP_BACKEND_URL from frontend/.env so every suite runs green
# without manual env exports.
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_BACKEND, ".env"))
except ImportError:
    pass
if not os.environ.get("REACT_APP_BACKEND_URL"):
    try:
        with open(os.path.join(os.path.dirname(_BACKEND), "frontend", ".env")) as _f:
            for _line in _f:
                if _line.startswith("REACT_APP_BACKEND_URL="):
                    os.environ["REACT_APP_BACKEND_URL"] = \
                        _line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass


@pytest.fixture(autouse=True, scope="session")
def _silence_outbound_notifications():
    """Stub the Telegram HTTP call so no test EVER reaches Bot API.

    We patch at the lowest level (`send_telegram`) so every helper
    (`notify_trade_opened`, `notify_trade_closed`, `notify_breakeven`,
    `notify_high_conf_signal`, `notify_sl_imminent`, `notify_circuit_breaker`)
    is automatically silenced — without each test having to remember.
    """
    # Make sure backend/.env is loaded for any test that imports notifier
    # before pytest discovery does.
    os.environ.setdefault("STOIC_TEST_MODE", "1")
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    try:
        import notifier  # type: ignore
    except Exception:
        # notifier may not be importable in a few unit-test-only contexts;
        # if it isn't, there's nothing to silence anyway.
        yield
        return

    original = notifier.send_telegram

    async def _noop_send_telegram(user_id, event_type, title, lines):  # noqa: ARG001
        # Return False so callers know nothing was sent (matches real-life behaviour
        # when a user has no Telegram configured).
        return False

    notifier.send_telegram = _noop_send_telegram
    try:
        yield
    finally:
        notifier.send_telegram = original


_loop_ref: dict = {"loop": None}


def _shared_loop():
    """Round 9 item 10 — EXPLICIT event-loop management. No test may rely on
    the deprecated `asyncio.get_event_loop()` implicit-creation behaviour;
    this holder owns one long-lived loop for the whole suite and recreates it
    (resetting the motor client singletons, which would otherwise stay bound
    to a dead loop) only when the previous one was closed."""
    import asyncio
    loop = _loop_ref["loop"]
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _loop_ref["loop"] = loop
        try:
            import database as _dbmod
            _dbmod._client = None
            _dbmod._db = None
        except Exception:
            pass
    return loop


def run_async(coro):
    """Run a coroutine on the suite's shared loop (sync test helper)."""
    return _shared_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    """Guarantee a usable current loop per test — explicitly created and
    installed, never implicitly via deprecated `asyncio.get_event_loop()`.
    The motor singletons are reset per test: a client created inside one
    test's loop (`asyncio.run` or the shared loop) must never be reused on
    a different loop by the next test."""
    import asyncio
    asyncio.set_event_loop(_shared_loop())
    try:
        import database as _dbmod
        _dbmod._client = None
        _dbmod._db = None
    except Exception:
        pass
    yield


def _refuse_live_without_env_credentials(items):
    """audit round 7 P1 — live HTTP suites need env-provided admin credentials.
    Hard, non-zero, pre-network refusal (never a silent skip)."""
    live = [i for i in items if "/tests/integration/" not in str(i.fspath).replace("\\", "/")
            and "/tests/unit/" not in str(i.fspath).replace("\\", "/")]
    if not live or not os.environ.get("REACT_APP_BACKEND_URL"):
        return
    from live_target import admin_credentials
    try:
        admin_credentials(strict=True)
    except RuntimeError as e:
        pytest.exit(f"live suites refused: {e}", returncode=3)


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """Auto-marks by directory so default suites are self-contained:
        pytest -m unit         → pure unit tests (no DB, no backend)
        pytest -m integration  → MongoDB-backed tests
        pytest -m http         → live-backend HTTP tests (everything else)

    Live-test safety gate (audit item 43): every http-live test that is not
    explicitly marked `read_only` is treated as mutating —
      · requires STOIC_ALLOW_MUTATING_TESTS=YES (demo/staging envs only)
      · is FORBIDDEN against a LIVE environment (APP_ENV=production/prod or
        STOIC_ENVIRONMENT=live) unless marked `live_authorized`.
    Unit and integration lanes are never gated.
    """
    from pathlib import Path
    _refuse_live_without_env_credentials(items)
    root = Path(__file__).resolve().parent
    env_live = (
        os.environ.get("APP_ENV", "").strip().lower() in ("production", "prod")
        or os.environ.get("STOIC_ENVIRONMENT", "").strip().lower() == "live")
    allow = os.environ.get(
        "STOIC_ALLOW_MUTATING_TESTS", "").strip().upper() == "YES"
    skip_flag = pytest.mark.skip(
        reason="mutating live test — set STOIC_ALLOW_MUTATING_TESTS=YES "
               "(safety category: DEMO_MUTATING by default; mark read_only "
               "for pure-GET tests)")
    skip_live = pytest.mark.skip(
        reason="mutating test FORBIDDEN against a LIVE environment — add the "
               "live_authorized marker only with explicit authorization")
    for item in items:
        try:
            rel = Path(str(item.fspath)).resolve().relative_to(root)
        except ValueError:
            continue
        top = rel.parts[0] if rel.parts else ""
        if top == "unit":
            item.add_marker(pytest.mark.unit)
        elif top == "integration":
            item.add_marker(pytest.mark.integration)
        else:
            item.add_marker(pytest.mark.http)
            if item.get_closest_marker("read_only"):
                continue
            if env_live and not item.get_closest_marker("live_authorized"):
                item.add_marker(skip_live)
            elif not allow:
                item.add_marker(skip_flag)


@pytest.fixture(autouse=True, scope="session")
def _auto_csrf_header():
    """The backend enforces double-submit CSRF on cookie-authenticated
    mutations. Echo the csrf_token cookie into the X-CSRF-Token header on
    every `requests` call — exactly what the real frontend interceptor
    does — so the ~2k HTTP tests exercise the real mechanism."""
    try:
        import requests
    except ImportError:
        yield
        return
    original = requests.sessions.Session.request

    _bypass = os.environ.get("RATE_LIMIT_BYPASS_TOKEN")
    _stepup = os.environ.get("STEP_UP_BYPASS_TOKEN")

    def patched(self, method, url, **kwargs):
        # CI targets plain http://127.0.0.1 — requests refuses to SEND
        # cookies flagged Secure over http. The flag is a browser transport
        # concern; strip it client-side so the jar keeps working.
        if str(url).startswith("http://"):
            for c in self.cookies:
                c.secure = False
        headers = kwargs.get("headers") or {}
        if _bypass:
            headers.setdefault("X-RateLimit-Bypass", _bypass)
        # Step-up MFA bypass for the legacy HTTP suite. Tests that want the
        # REAL step-up gate set session.headers["X-Step-Up-Bypass"] = "".
        if _stepup and "X-Step-Up-Bypass" not in self.headers:
            headers.setdefault("X-Step-Up-Bypass", _stepup)
        if method.upper() not in ("GET", "HEAD", "OPTIONS"):
            token = self.cookies.get("csrf_token")
            if not token:
                jar = kwargs.get("cookies")
                if isinstance(jar, dict):
                    token = jar.get("csrf_token")
                elif jar is not None:
                    token = getattr(jar, "get", lambda *_: None)("csrf_token")
            if token:
                headers.setdefault("X-CSRF-Token", token)
        if headers:
            kwargs["headers"] = headers
        return original(self, method, url, **kwargs)

    requests.sessions.Session.request = patched
    try:
        yield
    finally:
        requests.sessions.Session.request = original


