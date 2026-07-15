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


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    """Legacy tests call `asyncio.get_event_loop().run_until_complete(...)`.

    After any earlier test used `asyncio.run()` (which clears the current
    loop) that pattern raises `RuntimeError: There is no current event loop`
    in full-suite runs while passing in isolation. Guarantee a usable loop
    per test; when a NEW loop must be created, reset the motor singletons so
    the cached client is never bound to a dead loop.
    """
    import asyncio
    need_new = False
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            need_new = True
    except RuntimeError:
        need_new = True
    if need_new:
        asyncio.set_event_loop(asyncio.new_event_loop())
        try:
            import database as _dbmod
            _dbmod._client = None
            _dbmod._db = None
        except Exception:
            pass
    yield
