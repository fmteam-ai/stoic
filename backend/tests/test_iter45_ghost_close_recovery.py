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


def test_anti_tilt_uses_strict_negative():
    """All 4 gating/status sites must count a loss ONLY when pnl < 0 —
    zero-P&L ghost closes froze accounts with no real losing trades."""
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel, expect in (("bot_runner.py", 1), ("routes/bot_routes.py", 2),
                        ("routes/diagnostic_routes.py", 1)):
        src = open(os.path.join(backend, rel)).read()
        assert 'or 0) <= 0 for r in' not in src, f"{rel} still counts pnl==0 as a loss"
        assert src.count('or 0) < 0 for r in') >= expect, rel


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
