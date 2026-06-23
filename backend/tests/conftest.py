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
