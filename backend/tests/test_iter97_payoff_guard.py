"""iter-57 · Payoff repair — payoff guard + intraday counter-momentum gate.

Born from 2026-07-06: 78.9% win rate but NEGATIVE profit (avg win $28.57 vs
avg loss $111.35; 147/147 trades were SELLs while gold rallied intraday)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from payoff_guard import payoff_guard_apply, intraday_counter_momentum  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sig(entry, sl, tp1, action="SELL"):
    return {"entry_price": entry, "stop_loss": sl, "tp1": tp1, "action": action}


class TestPayoffGuard:
    def test_tightens_inverted_rr_sell(self):
        # The real 2026-07-06 setup: entry 4138, SL 15 above, TP1 6 below (2.5x)
        r = payoff_guard_apply(sig(4138.0, 4153.0, 4132.0), {})
        assert r and r.get("tighten") == 4150.0  # entry + 2x6
        assert "tightened" in r["reason"]
        assert abs(r["sl_pips_scale"] - 12.0 / 15.0) < 1e-9

    def test_tightens_inverted_rr_buy(self):
        r = payoff_guard_apply(sig(4138.0, 4123.0, 4144.0, action="BUY"), {})
        assert r and r.get("tighten") == 4126.0  # entry - 2x6

    def test_skip_mode(self):
        r = payoff_guard_apply(sig(4138.0, 4153.0, 4132.0), {"payoff_guard_mode": "skip"})
        assert r and "Trade skipped" in r.get("skip", "")

    def test_allows_healthy_rr(self):
        # SL 10 away, TP1 6 away → ratio 1.67 ≤ 2 → no action
        assert payoff_guard_apply(sig(4100.0, 4110.0, 4094.0), {}) is None

    def test_boundary_exactly_2x_allowed(self):
        assert payoff_guard_apply(sig(4100.0, 4110.0, 4095.0), {}) is None

    def test_custom_ratio(self):
        s = sig(4100.0, 4110.0, 4096.0)  # SL 10, TP1 4 → 2.5x
        assert payoff_guard_apply(s, {}) is not None
        assert payoff_guard_apply(s, {"payoff_guard_max_sl_tp1": 3.0}) is None

    def test_disabled_via_cfg(self):
        s = sig(4138.0, 4153.0, 4132.0)
        assert payoff_guard_apply(s, {"payoff_guard_enabled": False}) is None

    def test_falls_back_to_take_profit(self):
        s = {"entry_price": 4138.0, "stop_loss": 4153.0, "take_profit": 4132.0, "action": "SELL"}
        assert payoff_guard_apply(s, {}) is not None

    def test_missing_levels_no_action(self):
        assert payoff_guard_apply({"entry_price": 4100.0, "action": "SELL"}, {}) is None


H = [
    {"date": "2026-07-04", "close": 4100.0},
    {"date": "2026-07-05", "close": 4110.0},   # yesterday's close (ref)
    {"date": "2026-07-06", "close": 4142.0},   # today's forming bar — must be skipped
]


class TestIntradayCounterMomentum:
    def test_sell_vetoed_on_intraday_rally(self):
        # current 4143 vs yesterday 4110 = +0.80% intraday — SELL fights the tape
        veto, chg = intraday_counter_momentum("SELL", "XAUUSD", 4143.0, H, today="2026-07-06")
        assert veto is not None and chg == 0.803

    def test_buy_allowed_on_rally(self):
        veto, chg = intraday_counter_momentum("BUY", "XAUUSD", 4143.0, H, today="2026-07-06")
        assert veto is None and chg == 0.803

    def test_buy_vetoed_on_intraday_selloff(self):
        veto, _ = intraday_counter_momentum("BUY", "XAUUSD", 4090.0, H, today="2026-07-06")
        assert veto is not None

    def test_small_move_allows_both(self):
        assert intraday_counter_momentum("SELL", "XAUUSD", 4112.0, H, today="2026-07-06")[0] is None
        assert intraday_counter_momentum("BUY", "XAUUSD", 4108.0, H, today="2026-07-06")[0] is None

    def test_forming_bar_skipped_as_reference(self):
        # If today's bar were used as ref, chg would be ~0 and no veto could fire.
        _, chg = intraday_counter_momentum("SELL", "XAUUSD", 4143.0, H, today="2026-07-06")
        assert abs(chg) > 0.5

    def test_crypto_higher_threshold(self):
        h = [{"date": "2026-07-05", "close": 100000.0}]
        assert intraday_counter_momentum("SELL", "BTCUSD", 100600.0, h, today="2026-07-06")[0] is None  # +0.6% < 1.0%
        assert intraday_counter_momentum("SELL", "BTCUSD", 101100.0, h, today="2026-07-06")[0] is not None

    def test_no_history_no_veto(self):
        assert intraday_counter_momentum("SELL", "XAUUSD", 4143.0, [], today="2026-07-06") == (None, None)


class TestShortTierMomentumVeto:
    def _tiers(self, slope, rsi=70):
        return {"SHORT": {"direction": "UP" if slope > 0 else "DOWN",
                          "sma_fast_slope_pct": slope, "rsi": rsi}}

    def test_sell_vetoed_on_strong_weekly_rally(self):
        from payoff_guard import short_tier_momentum_veto
        r = short_tier_momentum_veto("SELL", self._tiers(1.42))
        assert r is not None and "fresh rally" in r

    def test_sell_allowed_on_weak_up_slope(self):
        from payoff_guard import short_tier_momentum_veto
        assert short_tier_momentum_veto("SELL", self._tiers(0.6)) is None

    def test_buy_vetoed_on_strong_weekly_selloff(self):
        from payoff_guard import short_tier_momentum_veto
        assert short_tier_momentum_veto("BUY", self._tiers(-1.3)) is not None

    def test_buy_allowed_with_rally(self):
        from payoff_guard import short_tier_momentum_veto
        assert short_tier_momentum_veto("BUY", self._tiers(1.42)) is None

    def test_missing_tiers_no_veto(self):
        from payoff_guard import short_tier_momentum_veto
        assert short_tier_momentum_veto("SELL", {}) is None
        assert short_tier_momentum_veto("SELL", {"SHORT": {}}) is None

    def test_hold_no_veto(self):
        from payoff_guard import short_tier_momentum_veto
        assert short_tier_momentum_veto("HOLD", self._tiers(2.0)) is None


class TestWiring:
    def test_bot_runner_payoff_guard(self):
        src = open(os.path.join(BACKEND, "bot_runner.py")).read()
        assert "payoff_guard_apply" in src
        assert '"payoff_guard_tighten"' in src and '"payoff_guard_veto"' in src

    def test_ai_signals_intraday_gate(self):
        src = open(os.path.join(BACKEND, "ai_signals.py")).read()
        assert "intraday_counter_momentum" in src
        assert "VETO (intraday-momentum)" in src
        assert '"intraday_momentum"' in src
        assert "short_tier_momentum_veto" in src
        assert "VETO (short-tier-momentum)" in src
