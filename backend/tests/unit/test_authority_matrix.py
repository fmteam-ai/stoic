"""Unit — Global Trading Authority matrix (audit P0-1) + fail-closed
execution plane (audit P0-2). Pure/async with an in-memory fake db.

Matrix: every domain × every level. Assertions:
  · enforce_new_trade() and compute_authority() agree (same snapshot);
  · CLOSE_ONLY or worse in ANY domain → ok False (no dispatch);
  · an UNAVAILABLE (raising) domain → ok False;
  · REDUCED resizes only while hard-truth domains are FULL.
"""
import asyncio

import pytest

import trading_authority as ta
from trading_authority import (HARD_TRUTH_DOMAINS, LEVELS, compute_authority,
                               enforce_new_trade, level_severity)

DOMAINS = ["platform", "broker", "risk", "pamm", "execution",
           "position_truth", "infrastructure", "account"]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _patch_all_full(monkeypatch):
    async def full(*_a, **_k):
        return {"level": "FULL", "reason": "test"}
    for name in ("platform_domain", "broker_domain", "risk_domain",
                 "pamm_domain", "execution_domain",
                 "position_truth_domain", "infrastructure_domain",
                 "account_domain"):
        monkeypatch.setattr(ta, name, full)
    # _DOMAINS holds references — rebind
    monkeypatch.setattr(ta, "_DOMAINS", {
        "platform": ta.platform_domain, "broker": ta.broker_domain,
        "risk": ta.risk_domain, "pamm": ta.pamm_domain,
        "execution": ta.execution_domain,
        "position_truth": ta.position_truth_domain})


def _set(monkeypatch, domain: str, level: str):
    async def fn(*_a, **_k):
        return {"level": level, "reason": f"{domain} forced {level}"}
    if domain in ta._DOMAINS:
        ta._DOMAINS[domain] = fn
    else:
        monkeypatch.setattr(ta, f"{domain}_domain", fn)


def _raise(monkeypatch, domain: str):
    async def fn(*_a, **_k):
        raise RuntimeError("boom")
    if domain in ta._DOMAINS:
        ta._DOMAINS[domain] = fn
    else:
        monkeypatch.setattr(ta, f"{domain}_domain", fn)


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("level", LEVELS)
def test_matrix_every_domain_every_level(monkeypatch, domain, level):
    _patch_all_full(monkeypatch)
    _set(monkeypatch, domain, level)
    snap = _run(compute_authority(None, {"_id": "acc1"}))
    gate = _run(enforce_new_trade(None, {"_id": "acc1"}))
    assert snap["level"] == level
    assert snap["enforced_level"] == snap["level"]      # UI == choke point
    assert gate["domains"][domain] == level
    assert gate["snapshot_id"].startswith("authsnap_")
    if level_severity(level) >= level_severity("CLOSE_ONLY"):
        assert gate["ok"] is False and gate["level"] == level
    elif level == "REDUCED":
        if domain in HARD_TRUTH_DOMAINS:
            # hard-truth domain degraded → resizing is NOT allowed
            assert gate["ok"] is False and gate["level"] == "CLOSE_ONLY"
        else:
            assert gate["ok"] is True and gate["reduce_factor"] == 0.5
    else:
        assert gate["ok"] is True and gate["level"] == "FULL"


@pytest.mark.parametrize("domain", DOMAINS)
def test_unavailable_domain_refuses(monkeypatch, domain):
    _patch_all_full(monkeypatch)
    _raise(monkeypatch, domain)
    snap = _run(compute_authority(None, {"_id": "acc1"}))
    gate = _run(enforce_new_trade(None, {"_id": "acc1"}))
    assert domain in snap["unavailable_domains"]
    assert gate["ok"] is False
    assert any("unavailable" in r for r in gate["reasons"])


def test_worst_domain_wins_across_combination(monkeypatch):
    _patch_all_full(monkeypatch)
    _set(monkeypatch, "risk", "REDUCED")
    _set(monkeypatch, "pamm", "EMERGENCY")
    gate = _run(enforce_new_trade(None, {"_id": "acc1"}))
    assert gate["ok"] is False and gate["level"] == "EMERGENCY"
    assert any("pamm forced EMERGENCY" in r for r in gate["reasons"])


# ───────────── P0-2: fail-closed execution plane ─────────────────────────

class _NeverEngine:
    calls = 0

    async def execute_authorized(self, **_kw):
        _NeverEngine.calls += 1
        raise AssertionError("engine must NEVER be reached")


class _BrokenDb:
    """Non-Motor object: every attribute access raises AttributeError —
    exactly the shape the deleted bypass used to fail OPEN on."""
    def __getattr__(self, name):
        raise AttributeError(name)


def test_submit_intent_fails_closed_on_broken_db(monkeypatch):
    import execution_authority as ea
    import database
    monkeypatch.setattr(database, "get_db", lambda: _BrokenDb())
    monkeypatch.setattr(ea, "mint_authorization",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError("no authorization may be minted")),
                        raising=False)
    _NeverEngine.calls = 0
    out = _run(ea.submit_intent(
        user_id="u1", account={"_id": "acc1", "account_role": "STANDARD"},
        signal={"symbol": "XAUUSD", "action": "BUY", "lot_size": 0.1},
        engine=_NeverEngine()))
    assert out["blocked"] in ("pamm_resolution_error",
                              "intent_pipeline_error")
    assert out["correlation_id"].startswith("execfail_")
    assert _NeverEngine.calls == 0


def test_no_test_bypass_remains_in_execution_plane():
    import inspect
    import execution
    import execution_authority
    for mod in (execution, execution_authority):
        src = inspect.getsource(mod)
        assert "except (TypeError, AttributeError)" not in src, mod.__name__
        assert "mint_authorization(\"\")" not in src, mod.__name__
