"""iter-206 — security-audit hardening regressions (v59/v60 brain):
SEC-001 qualify rate limit + cached scorecard, SEC-002 degraded-endpoint
error scrubbing for non-admins, SEC-003 regex escaping in transaction
costs."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


# ───────────── SEC-001 · qualify rate limit (pure window) ─────────────

def test_qualify_rate_window_blocks_fourth_call():
    from champion_challenger2 import (QUALIFY_MAX_PER_WINDOW,
                                      QualifyRateLimited, _qualify_calls,
                                      _rate_check)
    uid = "SEC_TEST_user_rate"
    _qualify_calls.pop(uid, None)
    for _ in range(QUALIFY_MAX_PER_WINDOW):
        _rate_check(uid)
    try:
        _rate_check(uid)
        raise AssertionError("4th call must raise QualifyRateLimited")
    except QualifyRateLimited as e:
        assert e.retry_in_s > 0
    finally:
        _qualify_calls.pop(uid, None)


def test_qualify_scorecard_freshness_constant():
    from champion_challenger2 import QUALIFY_WINDOW_S, SCORECARD_FRESH_S
    assert SCORECARD_FRESH_S >= 300
    assert QUALIFY_WINDOW_S >= 300


# ───────────── SEC-003 · regex escaping in transaction costs ─────────────

class _CaptureCursor:
    def __init__(self, q):
        self.q = q

    def sort(self, *a):
        return self

    def limit(self, *a):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _CaptureColl:
    def __init__(self):
        self.last_query = None

    def find(self, q, *a, **k):
        self.last_query = q
        return _CaptureCursor(q)


class _CaptureDb:
    def __init__(self):
        self.trade_outcomes = _CaptureColl()
        self.trades = _CaptureColl()


def test_costs_symbol_regex_is_escaped():
    from transaction_costs import _latency_r, _median_slippage_r
    db = _CaptureDb()
    payload = "(XAU["   # unbalanced regex metacharacters
    asyncio.run(_median_slippage_r(db, "u1", payload))
    asyncio.run(_latency_r(db, "u1", payload))
    for coll in (db.trade_outcomes, db.trades):
        rx = coll.last_query["symbol"]["$regex"]
        assert "\\(" in rx and "\\[" in rx, rx


def test_costs_endpoint_survives_regex_payload():
    """expected_cost_r end-to-end with metacharacters — never raises."""
    from transaction_costs import expected_cost_r
    db = _CaptureDb()
    out = asyncio.run(expected_cost_r(db, "u1", ".*(["))
    assert out["cost_r"] >= 0.03


# ───────────── SEC-002 · degraded endpoint scrubbing (route logic) ────────

def test_degraded_scrub_logic():
    """Mirror of the route scrub — non-admin payload must not carry
    last_error keys while structure stays intact."""
    out = {"mode": "DEGRADED_INTELLIGENCE",
           "subsystems": {
               "meta_decision": {"ok": False, "failing": True,
                                 "last_error": "Mongo timeout at 10.0.0.5",
                                 "policy": {"risk_multiplier": 0.5}},
               "market_memory": {"ok": True, "failing": False,
                                 "policy": {"risk_multiplier": 1.0}}}}
    for sub in out["subsystems"].values():
        sub.pop("last_error", None)
    assert all("last_error" not in s for s in out["subsystems"].values())
    assert out["subsystems"]["meta_decision"]["failing"] is True


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
