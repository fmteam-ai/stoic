"""iter-130 · Narrative fusion — news as decision input + risk sizing.

Born 2026-07-13: gold slid 2% on US-Iran escalation; the news layer's
freshest headline was 3 days old (Fed-only queries) and news was veto-only.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_understanding import (news_confidence_bias, narrative_risk_scale,  # noqa: E402
                                _parse_rss, GEO_QUERY, RSS_FEEDS, CACHE_TTL)

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def snap(net, label="bearish", drivers=None):
    return {"net": net, "label": label, "headlines": 12,
            "drivers": drivers or [{"title": "top", "score": net}]}


class TestConfidenceBias:
    def test_aligned_boost(self):
        d, note = news_confidence_bias("SELL", snap(-1.5))
        assert d == 5.0 and "supports SELL" in note

    def test_opposed_cut(self):
        d, note = news_confidence_bias("BUY", snap(-1.5))
        assert d == -10.0 and "opposes BUY" in note

    def test_weak_narrative_no_bias(self):
        assert news_confidence_bias("BUY", snap(-0.8)) == (0.0, None)

    def test_no_snap(self):
        assert news_confidence_bias("BUY", None) == (0.0, None)


class TestNarrativeRiskScale:
    def test_against_moderate_narrative_half_size(self):
        s, r = narrative_risk_scale("BUY", snap(-1.5))
        assert s == 0.5 and "half size" in r

    def test_with_narrative_full_size(self):
        s, r = narrative_risk_scale("SELL", snap(-1.5))
        assert s == 1.0 and r is None

    def test_shock_headline_trims_everything(self):
        sh = snap(0.5, drivers=[{"title": "U.S. launches airstrikes against Iran",
                                 "score": 2.5}])
        s, r = narrative_risk_scale("SELL", sh)
        assert s == 0.7 and "shock" in r

    def test_against_narrative_plus_shock_floors(self):
        sh = snap(-1.5, drivers=[{"title": "airstrikes", "score": -2.6}])
        s, r = narrative_risk_scale("BUY", sh)
        assert abs(s - 0.35) < 1e-9  # 0.5 * 0.7 = 0.35 floor

    def test_quiet_tape_untouched(self):
        assert narrative_risk_scale("BUY", snap(0.2)) == (1.0, None)


class TestRssParser:
    XML = """<?xml version="1.0"?><rss><channel>
    <item><title>US-Iran conflict escalates in Hormuz</title>
    <pubDate>Mon, 13 Jul 2026 18:00:00 GMT</pubDate></item>
    <item><title></title></item>
    <item><title>Second story</title><pubDate>bogus</pubDate></item>
    </channel></rss>"""

    def test_parses_items(self):
        items = _parse_rss(self.XML, "BBC")
        assert len(items) == 2
        assert items[0]["title"].startswith("US-Iran")
        assert items[0]["publishedAt"].startswith("2026-07-13")
        assert items[1]["publishedAt"] == ""

    def test_bad_xml_returns_empty(self):
        assert _parse_rss("not xml at all", "X") == []


class TestConfig:
    def test_geo_query_covers_iran(self):
        for term in ("Iran", "sanctions", "ceasefire", "OPEC", "tariff"):
            assert term in GEO_QUERY

    def test_rss_feeds_and_fresh_cache(self):
        assert len(RSS_FEEDS) >= 2
        assert CACHE_TTL <= 15 * 60


class TestWiring:
    def test_bot_runner_fuses_narrative(self):
        src = open(os.path.join(BACKEND, "bot_runner.py")).read()
        for needle in ("news_confidence_bias", "narrative_risk_scale",
                       '"news_bias_applied"', '"news_size_trim"',
                       'signal.get("news_size_scale")'):
            assert needle in src, needle


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
