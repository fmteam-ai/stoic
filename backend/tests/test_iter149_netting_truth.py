"""iter-149 · EA v1.53 — netting truth + unresolved-accept lifecycle (P0):
(1) accepted order without a resolved position NEVER reported open;
(2) per-trade filled volume from OUR deals, never POSITION_VOLUME;
(3) partial-fill truth from deal volume vs requested, retcode diagnostic."""
import os as _os
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)

EA_PATH = _os.path.join(_BACKEND_DIR, "static", "EmergentTradingBridge.mq5")


def _src(path):
    with open(path) as f:
        return f.read()


def _body(src, start, end=None):
    i = src.index(start)
    j = src.index(end) if end else len(src)
    return src[i:j]


def _exec_body():
    return _body(_src(EA_PATH), "void ExecuteTrade(", "void ApplyFullClose(")


# ------------------- P0-1 · open requires a RESOLVED position identifier
def test_open_requires_resolved_position():
    et = _exec_body()
    assert "bool opened = (pos_id > 0);" in et
    assert "bool opened = (accepted || pos_id > 0);" not in et
    # unresolved-accept branch: pending ack + JR_ACCEPTED journal, no open
    assert "if (accepted && pos_id == 0) {" in et
    branch = et[et.index("if (accepted && pos_id == 0) {"):
                et.index("bool opened = (pos_id > 0);")]
    assert '"accepted_unresolved"' in branch
    assert 'JSet("T", trade_id, JR_ACCEPTED);' in branch
    assert "return;" in branch


def test_no_order_ticket_masquerading_as_position():
    et = _exec_body()
    # the fallback that reported an ORDER ticket as mt5_ticket must be gone
    assert "ulong report_ticket = (pos_id > 0 ? pos_id : res.order);" not in et
    assert "SendOpenReport(trade_id, pos_id, status" in et


def test_unresolved_redispatch_never_resends_order():
    et = _exec_body()
    h = et[et.index("if (jstate == JR_ACCEPTED) {"):et.index("if (jstate >= JR_TICKET")]
    assert "FindPositionForTrade(trade_id, rsym, action, lot)" in h
    assert "OrderSend" not in h, "JR_ACCEPTED redispatch must never resend"
    assert "ReportOpenFromJournal(trade_id);" in h
    assert '"accepted_unresolved"' in h  # still-unresolved replay ack


def test_broker_history_lag_retry():
    et = _exec_body()
    assert "for (int rtry = 0; rtry < 3 && accepted && pos_id == 0" in et
    assert "Sleep(300);" in et


# ------------------------ P0-2 · per-trade fill from OUR deals (netting)
def test_filled_volume_from_own_deals_not_position_volume():
    src = _src(EA_PATH)
    assert "double OrderFilledVolume(ulong order_ticket)" in src
    fn = _body(src, "double OrderFilledVolume(", "// v1.52 — select the live position")
    assert "DEAL_ORDER" in fn and "DEAL_VOLUME" in fn
    et = _exec_body()
    assert "double filled = OrderFilledVolume(res.order);" in et
    # POSITION_VOLUME must feed pos_volume (exposure), never filled
    assert "filled = PositionGetDouble(POSITION_VOLUME);" not in et
    assert "pos_volume = PositionGetDouble(POSITION_VOLUME);" in et


def test_position_volume_reported_separately():
    src = _src(EA_PATH)
    sor = _body(src, "void SendOpenReport(", "void ReportOpenFromJournal(")
    assert "position_volume" in sor
    assert "double pos_volume = 0" in sor  # default param


# --------------------------- P0-3 · partial truth from deal volume
def test_partial_flag_from_deal_volume_not_retcode():
    et = _exec_body()
    assert "bool partial = (filled > 0 && filled + half_step < req.volume);" in et
    assert "bool partial  = (ok && res.retcode == TRADE_RETCODE_DONE_PARTIAL);" not in et


# ------------------------------------------------ backend consumption
def test_backend_model_has_position_volume():
    import sys
    sys.path.insert(0, _BACKEND_DIR)
    from models import BridgeTradeReport
    assert "position_volume" in BridgeTradeReport.model_fields


def test_backend_handles_accepted_unresolved():
    br = _src(_os.path.join(_BACKEND_DIR, "routes", "bridge_routes.py"))
    assert '"broker_accepted_unresolved"' in br
    assert '== "accepted_unresolved"' in br
    # reservation stays: trade must remain pending, never failed/open
    assert '"ignored": f"already_{trade[\'status\']}"' in br
    assert '"accepted_unresolved")' in br  # intel counter
    assert 'update["position_volume"]' in br
    # mt5_ticket:0 from unresolved acks must never be stored
    assert "if payload.mt5_ticket:\n        update[\"mt5_ticket\"] = payload.mt5_ticket" in br


def test_execution_health_exposes_unresolved():
    bo = _src(_os.path.join(_BACKEND_DIR, "routes", "bot_routes.py"))
    assert '"unresolved_submissions": unresolved' in bo
    assert '"submission_state": "broker_accepted_unresolved"' in bo


def test_version_153():
    from ea_version import current_ea_version  # noqa — repo-relative import
    import sys
    sys.path.insert(0, _TESTS_DIR)
    v = current_ea_version()
    assert v >= "1.53"
    assert f'#property version   "{v}"' in _src(EA_PATH)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
