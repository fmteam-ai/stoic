"""v1.60.44 — the Scalp page's Shadow Performance block showed the lifetime EURUSD aggregate (735 samples, PF 0.65)
under an XAUUSD runner card: the page always requested symbol=EURUSD. Metrics/decisions now follow the selected
runner (symbol + account) and the response carries a per-setup breakdown so the current setup is shown separately."""
import asyncio
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _dec(symbol, acc, net, setup="range_break_momentum_v2", ts=1):
    return {"user_id": "u1", "symbol": symbol, "account_id": acc, "ts_ms": ts, "cost_pips": 0.3,
            "outcome": {"result": "target_first" if net > 0 else "stop_first", "net_pips": net, "gross_move_pips": net + 0.3},
            "setup": {"setup": setup}, "forecast": {"expected_spread_cost_pips": 0.2}, "execution_mode": "shadow"}


def test_metrics_are_scoped_to_symbol_and_account_with_a_per_setup_breakdown():
    import routes.scalp_routes as sr
    db = FakeDb()
    for i in range(6):
        db.scalp_decisions.rows.append(_dec("EURUSD", "accA", -0.7, ts=i))            # the lifetime EURUSD noise
    db.scalp_decisions.rows.append(_dec("EURUSD", "accA", 1.0, setup="old_v1", ts=10))
    db.scalp_decisions.rows.append(_dec("XAUUSD", "accB", 2.0, ts=20))
    db.scalp_decisions.rows.append(_dec("XAUUSD", "accB", -1.0, setup="old_v1", ts=21))
    user = {"id": "u1"}
    with patch.object(sr, "get_db", lambda: db):
        eur = _run(sr.metrics(symbol="EURUSD", account_id=None, setup=None, user=user))
        xau_none = _run(sr.metrics(symbol="XAUUSD", account_id="accA", setup=None, user=user))
        xau = _run(sr.metrics(symbol="XAUUSD", account_id="accB", setup=None, user=user))
        xau_cur = _run(sr.metrics(symbol="XAUUSD", account_id="accB", setup="range_break_momentum_v2", user=user))
    assert eur["n"] == 7 and eur["symbol"] == "EURUSD"
    assert xau_none == {"symbol": "XAUUSD", "account_id": "accA", "setup": None, "n": 0, "by_setup": {}}   # XAUUSD has no decisions on A
    assert xau["n"] == 2 and xau["account_id"] == "accB"
    assert xau["by_setup"]["range_break_momentum_v2"] == {"n": 1, "net_expectancy_pips": 2.0, "profit_factor": None}
    assert xau["by_setup"]["old_v1"]["n"] == 1 and xau["by_setup"]["old_v1"]["net_expectancy_pips"] == -1.0
    assert xau_cur["n"] == 1 and xau_cur["setup"] == "range_break_momentum_v2" and list(xau_cur["by_setup"]) == ["range_break_momentum_v2"]


def test_decisions_accept_account_scope():
    import routes.scalp_routes as sr
    db = FakeDb()
    db.scalp_decisions.rows += [_dec("XAUUSD", "accB", 2.0, ts=1), _dec("XAUUSD", "accA", 1.0, ts=2)]
    with patch.object(sr, "get_db", lambda: db):
        out = _run(sr.decisions(limit=50, symbol="XAUUSD", account_id="accB", user={"id": "u1"}))
    assert [d["account_id"] for d in out["decisions"]] == ["accB"]


def test_scalp_page_follows_the_selected_runner():
    src = open(os.path.join(ROOT, "frontend", "src", "pages", "Scalp.jsx"), encoding="utf-8").read()
    assert 'api.get("/scalp/metrics?symbol=EURUSD")' not in src and 'api.get("/scalp/decisions?limit=25&symbol=EURUSD")' not in src
    assert "api.get(`/scalp/metrics?symbol=${sym}${accQ}`)" in src and 'data-testid="scalp-metrics-scope"' in src
    assert 'data-testid="scalp-metrics-by-setup"' in src and '" (current)"' in src
