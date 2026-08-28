"""iter-43 · US30 / NAS100 equity-index support — unit + live-feed tests."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market import SYMBOL_MAP, asset_type_of, ttl_seconds_for_quote  # noqa: E402
from pip_utils import PIP_SIZE, PIP_VALUE_USD_PER_STANDARD_LOT  # noqa: E402
from portfolio.sectors import sector_for  # noqa: E402
from broker_symbol_detector import resolve_broker_symbol, KNOWN_BASES  # noqa: E402
from microstructure import is_market_closed  # noqa: E402
from datetime import datetime, timezone


class TestIndexWiring:
    def test_symbol_map_entries(self):
        assert SYMBOL_MAP["US30"] == {"asset": "index", "yh": "^DJI"}
        assert SYMBOL_MAP["NAS100"] == {"asset": "index", "yh": "^NDX"}
        assert asset_type_of("US30") == "index"

    def test_pip_specs_exist(self):
        for s in ("US30", "NAS100"):
            assert s in PIP_SIZE and s in PIP_VALUE_USD_PER_STANDARD_LOT

    def test_sector_mapping(self):
        assert sector_for("US30") == "equity_index"
        assert sector_for("NAS100") == "equity_index"

    def test_quote_ttl(self):
        assert ttl_seconds_for_quote("index") == 15

    def test_known_bases(self):
        assert "US30" in KNOWN_BASES and "NAS100" in KNOWN_BASES


class TestBrokerAliases:
    def test_resolves_suffix(self):
        assert resolve_broker_symbol("US30", ["US30.fx", "XAUUSD.fx"]) == "US30.fx"
        assert resolve_broker_symbol("NAS100", ["NAS100#"]) == "NAS100#"

    def test_resolves_broker_alias_names(self):
        assert resolve_broker_symbol("NAS100", ["USTEC", "EURUSD"]) == "USTEC"
        assert resolve_broker_symbol("NAS100", ["US100.m"]) == "US100.m"
        assert resolve_broker_symbol("US30", ["DJ30", "GOLD#"]) == "DJ30"
        assert resolve_broker_symbol("US30", ["WS30.cash"]) == "WS30.cash"

    def test_canonical_beats_alias(self):
        assert resolve_broker_symbol("NAS100", ["USTEC", "NAS100"]) == "NAS100"

    def test_not_offered_returns_none(self):
        assert resolve_broker_symbol("US30", ["EURUSD", "XAUUSD"]) is None


class TestMarketHours:
    def test_indices_respect_weekend_close(self):
        sat = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)  # Saturday
        assert is_market_closed("US30", now=sat) is not None
        tue = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)  # Tuesday
        assert is_market_closed("NAS100", now=tue) is None


@pytest.mark.asyncio
class TestLiveFeeds:
    async def test_live_quote_and_history(self):
        from market import get_quote, get_history
        for s in ("US30", "NAS100"):
            q = await get_quote(s)
            assert q["price"] > 1000, f"{s} quote looks wrong: {q}"
            h = await get_history(s)
            assert len(h) >= 100, f"{s} history too short: {len(h)}"
            assert h[-1]["close"] > 1000


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
