"""Independent verification for SEC-001 v62.7 hardening.

Targets:
  * execution_authority._validate REJECTS risk-reducing labels on the
    open-trade plane (all documented flag variants).
  * A clean BUY still validates.
"""
from execution_authority import _validate


ACCOUNT = {"_id": "acct1", "mode": "paper"}
CLEAN_BUY = {"symbol": "EURUSD", "action": "BUY", "lot_size": 0.01}


def _has_risk_reducing_problem(problems):
    return any("risk-reducing" in p for p in problems)


class TestOpenPlaneRejectsLabels:
    def test_clean_buy_validates(self):
        assert _validate(CLEAN_BUY, ACCOUNT) == []

    def test_reduce_only_flag_rejected(self):
        sig = dict(CLEAN_BUY, reduce_only=True)
        assert _has_risk_reducing_problem(_validate(sig, ACCOUNT))

    def test_close_trade_flag_rejected(self):
        sig = dict(CLEAN_BUY, close_trade=True)
        assert _has_risk_reducing_problem(_validate(sig, ACCOUNT))

    def test_pamm_risk_reducing_flag_rejected(self):
        sig = dict(CLEAN_BUY, pamm_risk_reducing=True)
        assert _has_risk_reducing_problem(_validate(sig, ACCOUNT))

    def test_intent_close_rejected(self):
        sig = dict(CLEAN_BUY, intent="close")
        assert _has_risk_reducing_problem(_validate(sig, ACCOUNT))

    def test_intent_reduce_rejected(self):
        sig = dict(CLEAN_BUY, intent="reduce")
        assert _has_risk_reducing_problem(_validate(sig, ACCOUNT))

    def test_action_CLOSE_rejected_by_side_check(self):
        # action=CLOSE/REDUCE/FLATTEN fails the BUY/SELL side check even
        # before the risk-reducing gate — so still structurally rejected.
        for act in ("CLOSE", "REDUCE", "FLATTEN"):
            sig = dict(CLEAN_BUY, action=act)
            probs = _validate(sig, ACCOUNT)
            assert any("BUY or SELL" in p for p in probs), (act, probs)

    def test_multiple_flags_all_reported_once(self):
        sig = dict(CLEAN_BUY, reduce_only=True, close_trade=True,
                   pamm_risk_reducing=True, intent="close")
        probs = _validate(sig, ACCOUNT)
        assert _has_risk_reducing_problem(probs)
