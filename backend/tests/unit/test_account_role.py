"""Unit — account_role execution rule (audit correction).

STANDARD → normal rules; PAMM_MASTER → PAMM guard chain (enforced in
submit_intent); PAMM_INVESTOR → MONITOR ONLY, Execution Authority LOCKED.
Pure logic, no DB / network.
"""
from execution_authority import account_execution_lock


def test_standard_role_unlocked():
    assert account_execution_lock({"account_role": "STANDARD"}) is None
    assert account_execution_lock({}) is None            # legacy docs
    assert account_execution_lock(None) is None
    assert account_execution_lock({"account_role": None}) is None


def test_pamm_master_not_locked_here():
    # master is not LOCKED — it must flow through the PAMM guard chain,
    # which submit_intent enforces separately (pamm_master_unbound).
    assert account_execution_lock({"account_role": "PAMM_MASTER"}) is None


def test_pamm_investor_locked():
    reason = account_execution_lock({"account_role": "PAMM_INVESTOR"})
    assert reason and "MONITOR ONLY" in reason and "LOCKED" in reason
    # case-insensitive defence
    assert account_execution_lock({"account_role": "pamm_investor"})
