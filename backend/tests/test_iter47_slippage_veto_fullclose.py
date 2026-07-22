"""iter-47 · Slippage-veto repair + FULL_CLOSE queue consumption (EA v1.40).

Root causes fixed:
1. EA never consumed FULL_CLOSE pending_modifications — every slippage-veto
   force-close sat in the queue forever ("1 modification waiting >5min").
2. Suffixed symbols (GOLD#, XAUUSD.fx, XAUUSD-ECN) fell back to the 0.0001 FX
   pip — a $1 move on gold became "10000 pips", tripping the 9999 default cap.
3. The veto compared fill vs SIGNAL price (latency drift, not slippage) and
   counted favorable fills too — 100% of historical vetoes were false
   positives on profitable trades.
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pip_utils import base_symbol, pip_size, price_to_pips  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(BACKEND, rel)).read()


class TestBaseSymbol:
    def test_suffixed_gold_variants(self):
        for sym in ("XAUUSD.fx", "XAUUSD-ECN", "XAUUSD.raw", "XAUUSDpro", "XAUUSD#"):
            assert base_symbol(sym) == "XAUUSD", sym
            assert pip_size(sym) == 0.10, sym

    def test_broker_alias_gold(self):
        assert base_symbol("GOLD#") == "XAUUSD"
        assert pip_size("GOLD#") == 0.10

    def test_silver_alias(self):
        assert base_symbol("SILVER#") == "XAGUSD"

    def test_btc_and_fx_unchanged(self):
        assert pip_size("BTCUSD") == 1.00
        assert pip_size("EURUSD") == 0.0001
        assert pip_size("USDJPY.fx") == 0.01

    def test_price_to_pips_suffixed(self):
        # $1.06 on GOLD# must be 10.6 pips, NOT 10600
        assert abs(price_to_pips("GOLD#", 1.06) - 10.6) < 1e-9

    def test_suffixed_fx_majors_resolve(self):
        # OnEquity publishes EURUSD as EURUSD# — the scalp fast path and
        # candle store must both resolve it to the STOIC base, otherwise
        # ticks are dropped ("not in approved universe") and candles land
        # in an orphaned EURUSD# document.
        for sym in ("EURUSD#", "EURUSD.r", "EURUSDm", "EURUSD-ECN"):
            assert base_symbol(sym) == "EURUSD", sym
        assert base_symbol("GBPUSD#") == "GBPUSD"
        assert base_symbol("AUDUSD.raw") == "AUDUSD"
        assert base_symbol("TESTSYM") == "TESTSYM"  # unknowns untouched


class TestSlippageVeto:
    def test_direction_aware_and_true_baseline(self):
        src = _src("routes/bridge_routes.py")
        assert "payload.requested_price" in src
        assert "adverse" in src
        # veto gated on true-slippage measurement (EA v1.40+)
        assert "requested > 0" in src
        # cap lookup normalises the broker symbol
        assert "base = base_symbol(symbol)" in src

    def test_close_paths_clear_pending_modification(self):
        src = _src("routes/bridge_routes.py")
        # /bridge/report closed path + external-deal full close
        assert src.count('"pending_modification": None') >= 2 or (
            src.count('update["pending_modification"] = None') >= 1
            and src.count('"pending_modification": None') >= 1
        )


class TestEaV140:
    def test_versions(self):
        from ea_version import current_ea_version
        v = current_ea_version()
        ea = _src("static/EmergentTradingBridge.mq5")
        assert f'#property version   "{v}"' in ea
        assert f'#define EA_CLIENT_VERSION "{v}"' in ea
        assert f'LATEST_EA = "{v}"' in _src("routes/bot_routes.py")
        assert f'LATEST_EA = "{v}"' in _src("routes/diagnostic_routes.py")
        assert f'"ea_latest_version": "{v}"' in _src("routes/setup_routes.py")
        fe = open(_os.path.join(_REPO_DIR, "frontend", "src/pages/Accounts.jsx")).read()
        assert f'LATEST_EA_VERSION = "{v}"' in fe

    def test_full_close_consumed(self):
        ea = _src("static/EmergentTradingBridge.mq5")
        assert 'mod_type == "FULL_CLOSE"' in ea
        assert "ApplyFullClose" in ea
        # already-gone positions must still ack so the queue clears
        assert "already_closed" in ea

    def test_report_carries_requested_price(self):
        ea = _src("static/EmergentTradingBridge.mq5")
        assert '\\"requested_price\\"' in ea

    def test_stale_hint_updated(self):
        src = _src("routes/bot_routes.py")
        assert "Recompile EA to v1.26" not in src
        assert "v1.40+" in src


class TestBackfillTimestamps:
    """iter-48 · historical deals keep broker time; live deals keep server UTC.
    iter-51 · broker-LOCAL epochs corrected to true UTC via learned offset."""

    def test_external_deal_uses_broker_time_for_backfills(self):
        src = _src("routes/bridge_routes.py")
        assert "payload.backfill or age_sec > 600" in src
        assert "int(broker_deal_epoch) - offset" in src

    def test_offset_learned_from_live_deals(self):
        src = _src("routes/bridge_routes.py")
        assert "broker_utc_offset_sec" in src
        assert "round(raw_off / 900.0) * 900" in src   # snap to 15-min tz offsets
        assert "abs(snapped) <= 50400" in src           # ±14h sanity clamp

    def test_model_has_backfill_flag(self):
        assert "backfill: bool = False" in _src("models.py")

    def test_ea_tags_sweep_and_deep_sync_as_backfill(self):
        ea = _src("static/EmergentTradingBridge.mq5")
        # both SweepDealHistory and PushDealById bodies carry the tag;
        # the live OnTradeTransaction body must NOT.
        assert ea.count('\\"backfill\\":true') == 2

    def test_history_mode_applies_status_filter(self):
        fe = open(_os.path.join(_REPO_DIR, "frontend", "src/pages/Trades.jsx")).read()
        assert "historySummary && filter && !CLIENT_ONLY_FILTERS.includes(filter) && t.status !== filter" in fe
