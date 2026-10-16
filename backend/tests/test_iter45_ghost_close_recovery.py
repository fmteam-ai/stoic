"""iter-45 · Ghost-close P&L recovery + anti-tilt false-freeze fix."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trade_reconciler import _infer_close_reason  # noqa: E402


class TestInferCloseReason:
    T = {"entry_price": 4157.84, "stop_loss": 4169.34, "take_profit": 4153.90,
         "action": "SELL"}

    def test_tp_hit(self):
        assert _infer_close_reason(self.T, 4153.95) == "take_profit_reconciled"

    def test_sl_hit(self):
        assert _infer_close_reason(self.T, 4169.10) == "stop_loss_reconciled"

    def test_mid_exit(self):
        assert _infer_close_reason(self.T, 4160.00) == "broker_reconciled_estimated"

    def test_missing_data(self):
        assert _infer_close_reason({}, None) == "broker_reconciled_estimated"


def test_anti_tilt_uses_one_shared_helper():
    """#39 — every gating/status site calls anti_tilt.evaluate (origin=auto + is_streak_loss); the old
    all(pnl<0)-over-any-trade banner rule is gone. Zero-P&L ghost closes never count (pnl >= 0 → not a loss)."""
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ("bot_runner.py", "routes/bot_routes.py", "routes/diagnostic_routes.py"):
        src = open(os.path.join(backend, rel)).read()
        assert "anti_tilt_evaluate(" in src, rel
        assert "or 0) < 0 for r in" not in src and "or 0) <= 0 for r in" not in src, f"{rel} still has the old rule"
    from streak_losses import classify_loss
    assert classify_loss({"pnl": 0.0})["counts"] is False


def test_reconciler_estimates_from_live_snapshot():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "trade_reconciler.py")).read()
    assert "live_pnl" in src and "pnl_estimated" in src and "pnl_unknown" in src


def test_heartbeat_stamps_live_pnl():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "routes", "bridge_routes.py")).read()
    assert '"live_pnl": float(p.profit or 0.0)' in src
    # estimated exits must remain overwritable by the real broker deal
    assert "real_exit_known" in src
    assert 'update["pnl_estimated"] = False' in src


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
