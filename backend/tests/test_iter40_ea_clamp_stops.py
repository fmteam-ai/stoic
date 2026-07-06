"""iter-40 · EA v1.38 — retcode 10016 (INVALID_STOPS) fix.

Source-level assertions that the EA clamps SL/TP to the broker's minimum
stop distance (SYMBOL_TRADE_STOPS_LEVEL / freeze level / live spread),
normalises with the traded symbol's digits, retries once on 10016, and
that every routing surface advertises v1.38.
"""
import os
import re

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EA_PATH = os.path.join(BACKEND, "static", "EmergentTradingBridge.mq5")


def _src(path):
    with open(path) as f:
        return f.read()


def test_clamp_helper_exists_and_uses_broker_rules():
    src = _src(EA_PATH)
    assert "void ClampStops(string sym, int side, double &sl, double &tp, int extra_mult)" in src
    assert "SYMBOL_TRADE_STOPS_LEVEL" in src
    assert "SYMBOL_TRADE_FREEZE_LEVEL" in src
    # digits come from the traded symbol, not the chart
    assert "SymbolInfoInteger(sym, SYMBOL_DIGITS)" in src


def test_execute_trade_clamps_and_retries_on_10016():
    src = _src(EA_PATH)
    body = src[src.index("void ExecuteTrade("):src.index("void ClosePosition(")] \
        if "void ClosePosition(" in src else src[src.index("void ExecuteTrade("):]
    assert body.count("ClampStops(") >= 2, "expected initial clamp + retry clamp"
    assert "TRADE_RETCODE_INVALID_STOPS" in body
    # old chart-digits normalisation must be gone from the open path
    assert "NormalizeDouble(sl, _Digits)" not in body
    assert "NormalizeDouble(tp, _Digits)" not in body


def test_modify_sl_clamps():
    src = _src(EA_PATH)
    idx = src.index("TRADE_ACTION_SLTP")
    window = src[max(0, idx - 1200):idx + 600]
    assert "ClampStops(" in window, "ApplyModifySL must clamp the SL move"


def test_version_138_everywhere():
    src = _src(EA_PATH)
    assert '#property version   "1.40"' in src
    assert '#define EA_CLIENT_VERSION "1.40"' in src
    assert 'LATEST_EA = "1.40"' in _src(os.path.join(BACKEND, "routes", "bot_routes.py"))
    assert 'LATEST_EA = "1.40"' in _src(os.path.join(BACKEND, "routes", "diagnostic_routes.py"))
    assert '"ea_latest_version": "1.40"' in _src(os.path.join(BACKEND, "routes", "setup_routes.py"))
    frontend = os.path.join(os.path.dirname(BACKEND), "frontend", "src", "pages", "Accounts.jsx")
    assert 'LATEST_EA_VERSION = "1.40"' in _src(frontend)


def test_10016_hint_mentions_ea_update():
    src = _src(os.path.join(BACKEND, "routes", "bot_routes.py"))
    m = re.search(r'if "10016" in err:\s+retcode_hint = \((.+?)\)', src, re.S)
    assert m and "v1.38" in m.group(1)
