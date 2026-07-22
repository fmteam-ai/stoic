"""iter-147 · EA v1.51 — OrderCheck preflight completed on EVERY OrderSend
path + broker volume normalization + execution-health endpoint wiring."""
import os

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EA_PATH = os.path.join(BACKEND, "static", "EmergentTradingBridge.mq5")


def _src(path):
    with open(path) as f:
        return f.read()


def _body(src, start, end=None):
    i = src.index(start)
    j = src.index(end) if end else len(src)
    return src[i:j]


def test_version_151():
    src = _src(EA_PATH)
    assert '#property version   "1.51"' in src
    assert '#define EA_CLIENT_VERSION "1.51"' in src


def test_shared_preflight_helper_exists():
    src = _src(EA_PATH)
    assert "bool PreflightOk(MqlTradeRequest &req, string &perr)" in src
    helper = _body(src, "bool PreflightOk(", "void ExecuteTrade(")
    assert "OrderCheck(req, chk)" in helper
    assert "preflight_failed:retcode=" in helper


def test_every_ordersend_path_preflighted():
    src = _src(EA_PATH)
    # ExecuteTrade keeps its inline OrderCheck (initial + retry pass)
    et = _body(src, "void ExecuteTrade(", "void ApplyFullClose(")
    assert et.count("OrderCheck(req, chk)") == 2
    # close/modify paths run the shared helper BEFORE OrderSend
    for fn, nxt in (("void ApplyFullClose(", "void ClosePosition("),
                    ("void ClosePosition(", "void ApplyModifySL("),
                    ("void ApplyModifySL(", "void ApplyPartialClose("),
                    ("void ApplyPartialClose(", None)):
        body = _body(src, fn, nxt)
        assert "PreflightOk(req, perr)" in body, f"{fn} missing preflight"
        assert body.index("PreflightOk(req, perr)") < body.index("OrderSend(req, res)"), \
            f"{fn} preflight must run before OrderSend"


def test_preflight_failures_ack_without_consuming_intent():
    src = _src(EA_PATH)
    fc = _body(src, "void ApplyFullClose(", "void ClosePosition(")
    pf = fc[fc.index("PreflightOk(req, perr)"):fc.index("bool ok = OrderSend")]
    assert '\\"retryable\\":true' in pf.replace('\\"', '\\"') or '"retryable\\":true' in pf
    assert "MarkIntentDone" not in pf
    ms = _body(src, "void ApplyModifySL(", "void ApplyPartialClose(")
    pf2 = ms[ms.index("PreflightOk(req, perr)"):ms.index("bool ok = OrderSend")]
    assert "MarkIntentDone" not in pf2


def test_execute_trade_normalizes_broker_volume():
    src = _src(EA_PATH)
    et = _body(src, "void ExecuteTrade(", "void ApplyFullClose(")
    assert "SYMBOL_VOLUME_STEP" in et
    assert "SYMBOL_VOLUME_MIN" in et
    assert "SYMBOL_VOLUME_MAX" in et
    assert "volume_below_broker_min" in et
    assert "req.volume       = norm_lot;" in et
    assert "NormalizeDouble(lot, 2)" not in et
    # normalization happens BEFORE the OrderCheck preflight
    assert et.index("SYMBOL_VOLUME_MIN") < et.index("OrderCheck(req, chk)")


def test_min_fencing_version_unchanged():
    # existing v1.50 EAs must keep working — only LATEST advertises 1.51
    br = _src(os.path.join(BACKEND, "routes", "bot_routes.py"))
    assert 'FENCING_MIN_EA = "1.50"' in br
    assert 'LATEST_EA = "1.51"' in br


def test_execution_health_endpoint_exists():
    br = _src(os.path.join(BACKEND, "routes", "bot_routes.py"))
    assert '@router.get("/execution-health")' in br
    for key in ("outbox", "worker_leases", "scalp_owners",
                "scalp_submission_slots", "lifecycle_state",
                "stuck_pending", "costs"):
        assert key in br, f"execution-health missing {key}"
