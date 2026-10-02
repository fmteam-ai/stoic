"""Fail-closed / fail-open-advisory decorators and their application to
the capital-protecting overlays. Pure unit tests (no DB, no network)."""
import asyncio
from unittest.mock import MagicMock

import pytest

import fail_closed as fc
from fail_closed import (block_on_error, fail_closed, fail_open_advisory,
                         guard_failure_counters)

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def setup_function(_):
    fc.reset_counters()


# ── decorator semantics ─────────────────────────────────────────────────
def test_fail_closed_async_passes_through_result():
    @fail_closed("g", block_reason="g_error")
    async def guard(x):
        return {"allow": True, "x": x}
    assert _run(guard(3)) == {"allow": True, "x": 3}
    assert guard_failure_counters() == {}


def test_fail_closed_async_returns_block_on_exception():
    @fail_closed("risk_x", block_reason="risk_x_error")
    async def guard():
        raise ValueError("boom")
    out = _run(guard())
    assert out["blocked"] == "risk_x_error" and out["fail_closed"] is True
    assert out["guard"] == "risk_x" and out["error_type"] == "ValueError"
    assert "boom" in out["error"] and "fail-closed" in out["reason"]
    assert guard_failure_counters() == {"closed:risk_x": 1}


def test_fail_closed_sync_and_custom_factory():
    @fail_closed("s", block_factory=lambda n, e: {"ok": False, "by": n})
    def guard(v):
        return 1 / v
    assert guard(2) == 0.5
    assert guard(0) == {"ok": False, "by": "s"}

    @fail_closed("hb", pass_args=True,
                 block_factory=lambda n, e, a, k: {"blocked": "x",
                                                   "arg": a[0]})
    def hb(v):
        raise TypeError("bad")
    assert hb("2020-??") == {"blocked": "x", "arg": "2020-??"}


def test_fail_closed_does_not_swallow_cancellation():
    @fail_closed("c")
    async def guard():
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        _run(guard())


def test_advisory_passes_through_and_defaults_on_exception():
    @fail_open_advisory("alpha", default=None)
    async def advise(v):
        if v is None:
            raise RuntimeError("down")
        return {"mode": v}
    assert _run(advise("WAIT")) == {"mode": "WAIT"}
    assert _run(advise(None)) is None

    @fail_open_advisory("timing", default=dict)
    def sync_advise():
        raise KeyError("k")
    assert sync_advise() == {}
    assert guard_failure_counters() == {"advisory:alpha": 1,
                                        "advisory:timing": 1}


def test_semantics_are_introspectable():
    @fail_closed("a")
    async def a():
        return 1

    @fail_open_advisory("b")
    async def b():
        return 1
    assert a.__guard_semantics__ == ("fail_closed", "a")
    assert b.__guard_semantics__ == ("fail_open_advisory", "b")
    assert asyncio.iscoroutinefunction(a)


def test_block_on_error_inline_form():
    out = block_on_error("risk_engine", ZeroDivisionError("x"),
                         "risk_engine_error")
    assert out["blocked"] == "risk_engine_error" and out["fail_closed"]
    assert guard_failure_counters() == {"closed:risk_engine": 1}


# ── applied overlays ────────────────────────────────────────────────────
def test_safety_guardian_exception_blocks():
    from safety_guardian import audit_pre_trade
    assert audit_pre_trade.__guard_semantics__ == ("fail_closed",
                                                  "safety_guardian")
    db = MagicMock()

    def _agg(*a, **k):
        raise RuntimeError("aggregate failed")
    db.trades.aggregate = _agg
    out = _run(audit_pre_trade(
        db=db, account={"_id": "a", "mode": "live", "equity": 10_000,
                        "balance": 10_000, "free_margin": 9_000},
        signal={"symbol": "EURUSD", "action": "BUY", "lot_size": 0.1,
                "entry_price": 1.1, "stop_loss": 1.09},
        user_id="u", cfg_account_id="a"))
    assert out["ok"] is False and out["blocked_by"] == "guardian_error"
    assert out["audit"][0]["ok"] is False


def test_safety_guardian_passes_paper_through():
    from safety_guardian import audit_pre_trade
    out = _run(audit_pre_trade(db=MagicMock(), account={"mode": "paper"},
                               signal={}, user_id="u", cfg_account_id=None))
    assert out["ok"] is True


def test_heartbeat_and_quote_freshness_fail_closed(monkeypatch):
    import execution
    assert execution.heartbeat_freshness_block("not-a-date") == {
        "blocked": "heartbeat_unparseable", "last_heartbeat": "not-a-date"}
    assert execution.heartbeat_freshness_block(
        "2000-01-01T00:00:00+00:00")["blocked"] == "stale_heartbeat"
    from datetime import datetime, timezone
    assert execution.heartbeat_freshness_block(
        datetime.now(timezone.utc).isoformat()) is None

    async def _boom(sym):
        raise RuntimeError("feed down")
    monkeypatch.setattr(execution, "get_quote", _boom)
    assert _run(execution.live_quote_price("EURUSD")) == 0.0   # → BLOCK

    async def _ok(sym):
        return {"price": 1.2345}
    monkeypatch.setattr(execution, "get_quote", _ok)
    assert _run(execution.live_quote_price("EURUSD")) == 1.2345


def test_std_risk_clamp_semantics():
    import bot_runner
    clamp = bot_runner._std_risk_clamp
    assert clamp.__guard_semantics__ == ("fail_closed", "std_risk_clamp")
    acct = {"equity": 10_000.0}
    # 1% of 10k = $100; 100 pips × $10 → max 0.10 lot
    out = clamp(acct, "XAUUSD", 2000.0, 1990.0, 0.5, 1.0)
    assert out["clamped"] and out["lot"] == pytest.approx(0.10)
    assert clamp(acct, "XAUUSD", 2000.0, 1990.0, 0.05, 1.0)["clamped"] \
        is False
    tiny = clamp({"equity": 50.0}, "XAUUSD", 2000.0, 1990.0, 0.05, 1.0)
    assert tiny.get("skip") is True
    bad = clamp(acct, "XAUUSD", "x", 1990.0, 0.05, 1.0)
    assert bad["fail_closed"] is True and bad["blocked"] == \
        "std_risk_clamp_error"


def test_std_risk_clamp_never_looser_with_broker_spec():
    import bot_runner
    # broker reports a SMALLER pip value than the static table: the clamp
    # keeps the larger (static) one — never loosens.
    acct = {"equity": 10_000.0, "symbol_specs": {"XAUUSD": {
        "point": 0.01, "tick_size": 0.01, "tick_value": 0.5,
        "contract_size": 50.0, "volume_min": 0.01, "volume_step": 0.01,
        "volume_max": 50}}}
    out = bot_runner._std_risk_clamp(acct, "XAUUSD", 2000.0, 1990.0, 0.5,
                                     1.0)
    assert out["lot"] == pytest.approx(0.10)


def test_advisory_overlays_are_decorated():
    import bot_runner
    assert bot_runner._execution_alpha_advice.__guard_semantics__[0] == \
        "fail_open_advisory"
    assert bot_runner._execution_timing_advice.__guard_semantics__[0] == \
        "fail_open_advisory"
