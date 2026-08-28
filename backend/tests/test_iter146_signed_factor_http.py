"""iter-146 signed-factor exposure HTTP verification (real guard path).

Reuses helpers from tests/test_iter221_risk_truth_http.py to:
  (a) BUY EURUSD 0.2 against open BUY GBPUSD 1.9 -> factor_exposure_exceeded
      on USD, detail.proposed_lots == 2.1 (net short), model == 'signed_net'.
  (b) SELL EURUSD 0.2 against the same seed -> USD nets toward zero
      (-1.9 + 0.2 = -1.7 abs 1.7 < 2.0), so factor_exposure is NOT the
      rejection reason (test allows any other reason or authorized).
"""
# ruff: noqa: F811, F401 — pytest fixtures imported from the iter221 suite
# are re-exported for collection and intentionally shadowed by test
# parameters (standard pytest pattern).
import uuid

import pytest

from tests.test_iter221_risk_truth_http import (  # noqa: E402
    _activate,
    _assign,
    _bind_master,
    _cleanup_account,
    _cleanup_program,
    _create_program,
    _guard_call,
    _insert_paper_account,
    _run,
    _validate,
    admin_id,  # fixture
    admin_session,  # fixture
    mongo,  # fixture
)

pytestmark = pytest.mark.http


class TestSignedFactorNettingHTTP:
    def _seed(self, admin_session, admin_id, mongo, suffix):
        pid = _create_program(admin_session, suffix)
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        _assign(admin_session, pid, "sniper", "controlled")
        _validate(admin_session, pid)
        _activate(admin_session, pid)
        mongo.trades.insert_one({
            "trade_id": f"iter146_fx_{uuid.uuid4().hex[:8]}",
            "account_id": acc_id, "status": "open",
            "symbol": "GBPUSD", "action": "BUY",
            "lot_size": 1.9, "risk_pct": 0.0})
        return pid, acc_id

    def test_buy_eurusd_signed_net_blocks_with_model_signed_net(
            self, admin_session, admin_id, mongo):
        pid, acc_id = self._seed(admin_session, admin_id, mongo, "sn_buy")
        try:
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.2,
                "strategy_id": "sniper"}))
            assert out["authorized"] is False, out
            assert out["reason"] == "factor_exposure_exceeded", out
            det = out["checks"][-1]["detail"]
            assert det["factor"] == "USD", det
            # signed-net: net short USD -2.1 -> abs == 2.1
            assert round(det["proposed_lots"], 4) == 2.1, det
            assert det.get("proposed_net_lots") == -2.1 or \
                round(det.get("proposed_net_lots", 0), 4) == -2.1, det
            assert det["model"] == "signed_net", det
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)

    def test_sell_eurusd_nets_out_no_factor_block(
            self, admin_session, admin_id, mongo):
        pid, acc_id = self._seed(admin_session, admin_id, mongo, "sn_sell")
        try:
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "SELL", "lot_size": 0.2,
                "strategy_id": "sniper"}))
            # Whatever the outcome (authorized or blocked on some other
            # gate), USD factor exposure must NOT be the rejection reason
            # because SELL EURUSD adds +0.2 to USD, netting -1.9+0.2=-1.7
            # which is inside the |2.0| cap.
            assert out.get("reason") != "factor_exposure_exceeded", out
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)
