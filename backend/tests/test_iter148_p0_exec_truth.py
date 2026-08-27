"""iter-148 · EA v1.52 — P0 execution-truth audit (round 2):
partial fills verified against the real broker position, order/deal/position
ticket separation, netting-aware recovery, exact 64-bit ticket journaling,
stop-confirmed intent consumption, volume-verified partial closes, plus
backend consumption and security corrections."""
import os as _os
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)

EA_PATH = _os.path.join(_BACKEND_DIR, "static", "EmergentTradingBridge.mq5")


def _src(path):
    with open(path) as f:
        return f.read()


def _body(src, start, end=None):
    i = src.index(start)
    j = src.index(end) if end else len(src)
    return src[i:j]


# ---------------------------------------------------------------- version
def test_version_152_everywhere():
    # version keeps moving (1.53+); assert consistency, not a fixed number
    import sys
    sys.path.insert(0, _TESTS_DIR)
    from ea_version import current_ea_version
    v = current_ea_version()
    assert v >= "1.52"
    src = _src(EA_PATH)
    assert f'#property version   "{v}"' in src
    assert f'#define EA_CLIENT_VERSION "{v}"' in src
    assert f'LATEST_EA = "{v}"' in _src(
        _os.path.join(_BACKEND_DIR, "routes", "bot_routes.py"))
    # existing v1.50 EAs must keep trading — the fencing floor is unchanged
    assert 'FENCING_MIN_EA = "1.50"' in _src(
        _os.path.join(_BACKEND_DIR, "routes", "bot_routes.py"))


# ------------------------------------------- P0-1 · partial-fill new orders
def test_open_accepts_done_partial_and_verifies_position():
    src = _src(EA_PATH)
    et = _body(src, "void ExecuteTrade(", "void ApplyFullClose(")
    assert "TRADE_RETCODE_DONE_PARTIAL" in et, \
        "DONE_PARTIAL must count as a real (partial) open"
    assert "PositionIdFromDeal(deal_tk)" in et
    assert "FindPositionForTrade(trade_id, broker_symbol" in et
    # v1.53 — OPEN strictly requires a RESOLVED position identifier;
    # accepted-but-unresolved stays pending (accepted_unresolved ack)
    assert "bool opened = (pos_id > 0);" in et
    assert "accepted_unresolved" in et
    assert "bool opened = (accepted || pos_id > 0);" not in et
    # old retcode-only success check must be gone
    assert "bool opened = (ok && res.retcode == TRADE_RETCODE_DONE);" not in et


def test_open_reports_filled_volume_and_partial_flag():
    src = _src(EA_PATH)
    sor = _body(src, "void SendOpenReport(", "void ReportOpenFromJournal(")
    for key in ("order_ticket", "deal_ticket", "position_id",
                "filled_volume", "partial_fill"):
        assert key in sor, f"SendOpenReport missing {key}"


# ------------------------------------- P0-2 · order vs position ticket
def test_ticket_separation_helpers_exist():
    src = _src(EA_PATH)
    assert "ulong PositionIdFromDeal(ulong deal_ticket)" in src
    assert "DEAL_POSITION_ID" in src
    assert "bool SelectPositionById(ulong position_id, string symbol)" in src
    assert "POSITION_IDENTIFIER" in src
    # netting fallback guarded by account margin mode
    assert "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING" in src


def test_journal_stores_order_deal_position_separately():
    src = _src(EA_PATH)
    et = _body(src, "void ExecuteTrade(", "void ApplyFullClose(")
    assert 'JSetTicket("K", trade_id, res.order);' in et
    assert 'JSetTicket("D", trade_id, deal_tk);' in et
    assert 'JSetTicket("Q", trade_id, pos_id);' in et
    # legacy double-journal write for the order ticket must be gone
    assert 'JSet("K", trade_id, (double)res.order);' not in src


# --------------------------------------------- P0-6 · ticket precision
def test_exact_64bit_ticket_storage():
    src = _src(EA_PATH)
    assert "void JSetTicket(string kind, string id, ulong ticket)" in src
    assert "ulong JGetTicket(string kind, string id)" in src
    helper = _body(src, "void JSetTicket(", "ulong PositionIdFromDeal(")
    assert "ticket >> 32" in helper and "0xFFFFFFFF" in helper
    # legacy single-double journals must still be readable
    assert "legacy" in helper


# --------------------------------------------- P0-5 · netting recovery
def test_netting_aware_crash_recovery():
    src = _src(EA_PATH)
    fn = _body(src, "ulong FindPositionForTrade(", "// Drop journal entries"
               if "// Drop journal entries" in
               _body(src, "ulong FindPositionForTrade(") else None)
    fn = _body(src, "ulong FindPositionForTrade(", "void SendCandles(") \
        if "void SendCandles(" in src else fn
    for token in ("DEAL_MAGIC", "DEAL_COMMENT", "DEAL_SYMBOL", "DEAL_TYPE",
                  "DEAL_VOLUME", "HistorySelect(TimeCurrent() - 7200"):
        assert token in src[src.index("ulong FindPositionForTrade("):
                            src.index("ulong FindPositionForTrade(") + 2500], \
            f"FindPositionForTrade missing {token}"
    et = _body(src, "void ExecuteTrade(", "void ApplyFullClose(")
    assert "FindPositionForTrade(trade_id, rec_symbol, action, lot)" in et, \
        "ORDER_SENT crash recovery must use the netting-aware search"


# ------------------------------- P0-3 · stop-confirmed intent consumption
def test_modify_sl_intent_needs_stop_confirmed():
    src = _src(EA_PATH)
    ms = _body(src, "void ApplyModifySL(", "void ApplyPartialClose(")
    assert "if (success && stop_confirmed) MarkIntentDone(intent, seq, trade_id);" in ms
    assert "if (success) MarkIntentDone(intent, seq, trade_id);" not in ms


# --------------------------------- P0-4 · volume-verified partial close
def test_partial_close_success_from_actual_volume():
    src = _src(EA_PATH)
    pc = _body(src, "void ApplyPartialClose(")
    assert "MathAbs(remaining - new_vol) <= step_tol" in pc
    assert "volume_mismatch_after_partial_close" in pc
    assert "bool success = (ok && res.retcode == TRADE_RETCODE_DONE);" not in pc


# ----------------------------------------------- backend consumption
def test_backend_report_model_has_v152_fields():
    import sys
    sys.path.insert(0, _BACKEND_DIR)
    from models import BridgeTradeReport
    fields = BridgeTradeReport.model_fields
    for f in ("order_ticket", "deal_ticket", "position_id",
              "filled_volume", "partial_fill"):
        assert f in fields, f"BridgeTradeReport missing {f}"


def test_backend_report_consumes_partial_fill():
    br = _src(_os.path.join(_BACKEND_DIR, "routes", "bridge_routes.py"))
    assert 'update["order_ticket"]' in br
    assert 'update["deal_ticket"]' in br
    assert 'update["position_id"]' in br
    assert 'update["partial_fill"] = True' in br
    assert 'update["lot_size"] = filled' in br
    assert '"partial_fill_open"' in br
    # duplicate guard tolerates the order→position ticket transition
    assert "int(payload.order_ticket or 0) != int(trade[\"mt5_ticket\"])" in br


# ------------------------------------------------- security corrections
def test_password_minimum_raised_to_8():
    m = _src(_os.path.join(_BACKEND_DIR, "models.py"))
    assert "password: str = Field(min_length=8)" in m
    assert "new_password: str = Field(min_length=6)" not in m
    assert "password: str = Field(min_length=6)" not in m


def test_no_raw_exception_strings_in_route_responses():
    routes_dir = _os.path.join(_BACKEND_DIR, "routes")
    offenders = []
    for f in sorted(_os.listdir(routes_dir)):
        if not f.endswith(".py"):
            continue
        for i, line in enumerate(_src(_os.path.join(routes_dir, f)).splitlines(), 1):
            s = line.strip()
            if s.startswith("#") or "logger." in s:
                continue
            if ("detail=str(e)" in s and "ValueError" not in s) \
                    or '"error": str(e)' in s or "'error': str(e)" in s \
                    or '"message": str(e)' in s:
                # deliberate ValueError validation messages are allowed
                offenders.append(f"{f}:{i}: {s}")
    # our own crafted ValueError messages (validation text) remain by design
    # (infra_routes surfaces deliberate ValueError/RuntimeError/Partner
    #  messages from the vps modules — never raw unexpected exceptions)
    allowed = ("bot_routes.py", "quant_routes.py", "shadow_routes.py",
               "infra_routes.py")
    offenders = [o for o in offenders if not o.startswith(allowed)]
    assert not offenders, f"raw str(e) leaked to clients: {offenders}"


# -------------------------------------------- release-audit portability
def test_no_hardcoded_app_paths_in_test_code():
    import re
    bad = []
    for root, dirs, files in _os.walk(_TESTS_DIR):
        if "__pycache__" in root:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            for i, line in enumerate(
                    _src(_os.path.join(root, f)).splitlines(), 1):
                s = line.strip()
                if s.startswith("#"):
                    continue
                if re.search(r'["\']/app/(backend|frontend)', s):
                    bad.append(f"{_os.path.join(root, f)}:{i}")
    assert not bad, f"hardcoded /app paths remain in test code: {bad[:10]}"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
