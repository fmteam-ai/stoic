"""iter-52 · Friday Flat guard — weekend gap protection."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from friday_flat import in_friday_flat_window, _tightened_sl, _ea_supports_full_close  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 2026-07-03 was a Friday
FRI = lambda h, m=0: datetime(2026, 7, 3, h, m, tzinfo=timezone.utc)  # noqa: E731
CFG = {"friday_flat_enabled": True, "friday_flat_minutes_before": 60}


class TestWindow:
    def test_inside_window(self):
        assert in_friday_flat_window(CFG, FRI(20, 30))["in_window"] is True
        assert in_friday_flat_window(CFG, FRI(20, 0))["in_window"] is True

    def test_before_window(self):
        assert in_friday_flat_window(CFG, FRI(19, 59))["in_window"] is False
        assert in_friday_flat_window(CFG, FRI(12, 0))["in_window"] is False

    def test_after_close_still_blocked(self):
        assert in_friday_flat_window(CFG, FRI(22, 0))["in_window"] is True

    def test_not_friday(self):
        mon = datetime(2026, 7, 6, 20, 30, tzinfo=timezone.utc)
        assert in_friday_flat_window(CFG, mon)["in_window"] is False

    def test_disabled(self):
        assert in_friday_flat_window({"friday_flat_enabled": False}, FRI(20, 30))["in_window"] is False

    def test_custom_minutes(self):
        cfg = {**CFG, "friday_flat_minutes_before": 240}
        assert in_friday_flat_window(cfg, FRI(17, 30))["in_window"] is True


class TestTightenedSl:
    def test_profitable_buy_goes_breakeven(self):
        tr = {"action": "BUY", "entry_price": 4100.0, "stop_loss": 4090.0, "live_price": 4110.0}
        assert _tightened_sl(tr) == 4100.0

    def test_losing_sell_halves_risk(self):
        tr = {"action": "SELL", "entry_price": 4100.0, "stop_loss": 4120.0, "live_price": 4110.0}
        assert _tightened_sl(tr) == 4115.0  # (4120+4110)/2

    def test_never_loosens(self):
        # SL already at breakeven for a profitable BUY → nothing to do
        tr = {"action": "BUY", "entry_price": 4100.0, "stop_loss": 4100.0, "live_price": 4110.0}
        assert _tightened_sl(tr) is None

    def test_no_sl_and_losing_returns_none(self):
        tr = {"action": "BUY", "entry_price": 4100.0, "stop_loss": 0, "live_price": 4090.0}
        assert _tightened_sl(tr) is None


class TestEaGate:
    def test_v140_supports(self):
        assert _ea_supports_full_close({"ea_version": "1.40"}) is True
        assert _ea_supports_full_close({"ea_version": "1.41"}) is True
        assert _ea_supports_full_close({"ea_version": "2.0"}) is True

    def test_older_degrades(self):
        assert _ea_supports_full_close({"ea_version": "1.39"}) is False
        assert _ea_supports_full_close({}) is False
        assert _ea_supports_full_close({"ea_version": "garbage"}) is False


class TestWiring:
    def test_bot_runner_gate_and_sweep(self):
        src = open(os.path.join(BACKEND, "bot_runner.py")).read()
        assert "in_friday_flat_window(cfg)" in src
        assert "sweep_friday_flat(db)" in src

    def test_config_fields(self):
        src = open(os.path.join(BACKEND, "models.py")).read()
        assert "friday_flat_enabled: bool = True" in src
        assert 'friday_flat_mode: str = "close"' in src

    def test_crypto_exempt_and_idempotent(self):
        src = open(os.path.join(BACKEND, "friday_flat.py")).read()
        assert "CRYPTO_BASES" in src
        assert '"friday_flat_at": {"$exists": False}' in src

    def test_frontend_ui(self):
        fe = open("/app/frontend/src/pages/BotConfig.jssx".replace("jssx", "jsx")).read()
        assert "friday_flat_enabled" in fe
        assert "friday-flat-mode" in fe
        assert "friday_flat_minutes_before" in fe
