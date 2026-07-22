"""iter-145 · Audit r4 Phase 1 — EA v1.50 execution integrity:
EA command fencing, durable new-order intent journal, actual-SL /
remaining-volume reporting, live-activation EA version gate."""
import asyncio
from datetime import datetime, timezone

EA = "/app/backend/static/EmergentTradingBridge.mq5"


def _ea():
    with open(EA) as f:
        return f.read()


def _src(p):
    with open(p) as f:
        return f.read()


def _fn(src, name, nxt):
    i = src.index(name)
    j = src.index(nxt, i)
    return src[i:j]


class TestEaVersionBump:
    def test_v150_everywhere(self):
        # iter-147 bumped LATEST to 1.51 — derive the current version so this
        # test keeps asserting version-consistency, not a hardcoded number.
        from ea_version import current_ea_version
        v = current_ea_version()
        assert v >= "1.50"
        src = _ea()
        assert f'#property version   "{v}"' in src
        assert f'#define EA_CLIENT_VERSION "{v}"' in src
        assert f'LATEST_EA = "{v}"' in _src("/app/backend/routes/bot_routes.py")
        assert f'LATEST_EA = "{v}"' in _src("/app/backend/routes/diagnostic_routes.py")
        assert f'"ea_latest_version": "{v}"' in _src("/app/backend/routes/setup_routes.py")
        assert f'LATEST_EA_VERSION = "{v}"' in _src("/app/frontend/src/pages/Accounts.jsx")
        assert f'LATEST_EA_VERSION = "{v}"' in _src("/app/frontend/src/components/EaVersionStrip.jsx")


def _strip_mql(src):
    """Remove string/char literals and comments so structure can be checked."""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':
            i += 1
            while i < n and src[i] != '"':
                i += 2 if src[i] == '\\' else 1
            i += 1
        elif c == "'":
            i += 1
            while i < n and src[i] != "'":
                i += 2 if src[i] == '\\' else 1
            i += 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


class TestEaStructuralSanity:
    """Compile-blocking corruption guards (edit fragments, stray braces)."""

    def test_braces_balance_outside_strings(self):
        s = _strip_mql(_ea())
        assert s.count("{") == s.count("}"), \
            f"brace imbalance: {s.count('{')} open vs {s.count('}')} close"

    def test_no_orphan_expression_lines_at_global_scope(self):
        # a GLOBAL-scope line beginning with '(' is always an edit fragment
        depth = 0
        for ln_no, line in enumerate(_strip_mql(_ea()).splitlines(), 1):
            if depth == 0:
                assert not line.lstrip().startswith("("), \
                    f"orphan fragment at global scope, stripped line {ln_no}: {line!r}"
            depth += line.count("{") - line.count("}")

    def test_file_ends_at_function_close(self):
        tail = _ea().rstrip()
        assert tail.endswith("}")
        # the final function must be self-balanced — nothing dangling after
        last_fn = _strip_mql(tail[tail.rindex("void ApplyPartialClose("):])
        assert last_fn.count("{") == last_fn.count("}")


class TestEaCommandFence:
    def test_journal_primitives(self):
        src = _ea()
        for token in ("#define JR_RECEIVED", "#define JR_ORDER_SENT",
                      "#define JR_TICKET", "#define JR_ACK_SENT",
                      "#define JR_FAILED", "string JKey(",
                      "bool IntentDone(", "void MarkIntentDone(",
                      "void SweepJournal()", "long FindPositionByComment("):
            assert token in src, token

    def test_sweep_called_on_init(self):
        body = _fn(_ea(), "int OnInit()", "void OnDeinit(")
        assert "SweepJournal();" in body

    def test_modifications_parse_intent_and_seq(self):
        body = _fn(_ea(), "void ParseModificationsBlock(",
                   "string ExtractString(")
        assert '"\\"intent_id\\":\\""' in body
        assert '"\\"seq\\":"' in body
        # object-scoped extraction — no cross-object key bleed
        assert "StringSubstr(section, t_end, obj_len)" in body

    def test_fence_replay_reack_and_stale_seq_skip(self):
        body = _fn(_ea(), "void ParseModificationsBlock(",
                   "string ExtractString(")
        assert "IntentDone(intent)" in body
        assert "intent_already_executed" in body
        assert '\\"replay\\":true' in body
        assert 'seq <= (long)JGet("S", trade_id)' in body

    def test_all_acks_echo_intent_and_mark_done(self):
        src = _ea()
        # signatures accept the fence params (compile-checked by user in F7)
        assert "void ApplyFullClose(string trade_id, long ticket, string intent = \"\", long seq = 0)" in src
        assert "void ApplyModifySL(string trade_id, long ticket, double new_sl,\n                   string intent = \"\", long seq = 0)" in src
        assert "void ApplyPartialClose(string trade_id, long ticket, double new_vol,\n                       string intent = \"\", long seq = 0)" in src
        for fn, nxt in (("void ApplyFullClose(", "void ClosePosition("),
                        ("void ApplyModifySL(", "void ApplyPartialClose(")):
            body = _fn(src, fn, nxt)
            assert '\\"intent_id\\":\\"%s\\"' in body, fn
            assert "MarkIntentDone(intent, seq, trade_id)" in body, fn
        pc = src[src.index("void ApplyPartialClose("):]
        assert '\\"intent_id\\":\\"%s\\"' in pc
        assert "MarkIntentDone(intent, seq, trade_id)" in pc


class TestEaIntentJournal:
    def _exec_body(self):
        return _fn(_ea(), "void ExecuteTrade(", "// ----- v1.40: FULL_CLOSE")

    def test_redispatch_replays_never_resends(self):
        body = self._exec_body()
        journal_gate = body[:body.index("OrderSend(req, res)")]
        assert "ReportOpenFromJournal(trade_id)" in journal_gate
        assert "jstate >= JR_TICKET" in journal_gate
        assert "journal_failed_replay" in journal_gate

    def test_crash_window_recovery_via_comment(self):
        body = self._exec_body()
        assert "FindPositionByComment(trade_id)" in body
        assert "order_sent_unconfirmed" in body
        assert "req.comment      = trade_id;" in body

    def test_journal_written_before_ordersend(self):
        body = self._exec_body()
        i_journal = body.index('JSet("T", trade_id, JR_ORDER_SENT)')
        i_send = body.index("bool ok = OrderSend(req, res)")
        assert i_journal < i_send

    def test_open_report_carries_actual_sl_fields(self):
        src = _ea()
        body = _fn(src, "void SendOpenReport(", "void ReportOpenFromJournal(")
        for f in ("requested_sl", "applied_sl", "confirmed_position_sl",
                  "replay"):
            assert f in body, f


class TestEaActualSlAndVolume:
    def test_modify_sl_acks_confirmed_stop(self):
        body = _fn(_ea(), "void ApplyModifySL(", "void ApplyPartialClose(")
        assert "PositionGetDouble(POSITION_SL)" in body
        assert "requested_sl" in body and "applied_sl" in body
        assert "confirmed_position_sl" in body

    def test_partial_close_acks_remaining_broker_volume(self):
        body = _ea()[_ea().index("void ApplyPartialClose("):]
        assert "PositionGetDouble(POSITION_VOLUME)" in body
        assert "remaining_volume" in body


class TestBackendConsumesV150:
    def test_poll_modifications_carry_intent_and_seq(self):
        src = _src("/app/backend/routes/bridge_routes.py")
        i = src.index("modifications.append({")
        seg = src[i:i + 600]
        assert '"intent_id": m.get("intent_id")' in seg
        assert '"seq": m.get("seq")' in seg

    def test_ack_model_has_v150_fields(self):
        from routes.bridge_routes import BridgeModificationAck
        f = BridgeModificationAck.model_fields
        for k in ("requested_sl", "applied_sl", "confirmed_position_sl",
                  "remaining_volume", "replay", "intent_id"):
            assert k in f, k

    def test_report_model_has_v150_fields(self):
        from models import BridgeTradeReport
        f = BridgeTradeReport.model_fields
        for k in ("requested_sl", "applied_sl", "confirmed_position_sl",
                  "replay"):
            assert k in f, k

    def test_ack_prefers_broker_confirmed_values(self):
        src = _src("/app/backend/routes/bridge_routes.py")
        assert "actual_sl = payload.confirmed_position_sl or payload.new_sl" in src
        assert 'update["confirmed_stop_loss"] = float(actual_sl)' in src
        assert "payload.remaining_volume" in src

    def test_report_stores_sl_audit_trail(self):
        src = _src("/app/backend/routes/bridge_routes.py")
        for k in ('update["requested_sl"]', 'update["applied_sl"]',
                  'update["open_ack_position_sl"]',
                  'update["journal_replayed_at"]'):
            assert k in src, k


class TestBrokerPreflight:
    """Phase 2 — broker-native OrderCheck() before every OrderSend."""

    def test_ordercheck_runs_before_ordersend_and_journal(self):
        body = _fn(_ea(), "void ExecuteTrade(", "// ----- v1.40: FULL_CLOSE")
        i_chk = body.index("OrderCheck(req, chk)")
        i_journal = body.index('JSet("T", trade_id, JR_ORDER_SENT)')
        i_send = body.index("bool ok = OrderSend(req, res)")
        assert i_chk < i_journal < i_send
        assert "MqlTradeCheckResult chk" in body
        assert "preflight_failed:retcode=%d" in body
        # rejection never reaches the broker and journals FAILED
        seg = body[i_chk:i_chk + 900]
        assert 'JSet("T", trade_id, JR_FAILED)' in seg
        assert "return;" in seg
        # broker comment is sanitised so the JSON body stays valid
        assert 'StringReplace(chk_comment, "\\"", "\'")' in body

    def test_backend_records_preflight_rejection(self):
        src = _src("/app/backend/routes/bridge_routes.py")
        assert 'update["preflight_rejected"] = True' in src
        assert 'update["preflight_error"]' in src
        assert '"broker_preflight_reject"' in src


class TestLiveActivationGate:
    def _account(self, ea_version):
        return {"mode": "live", "status": "connected",
                "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                "ea_version": ea_version, "equity": 1000}

    def test_paper_always_ready(self):
        from routes.bot_routes import _activation_readiness
        acc = self._account("1.30")
        acc["mode"] = "paper"
        assert asyncio.run(_activation_readiness(None, acc)) == []

    def test_old_ea_blocks_live(self):
        from routes.bot_routes import _activation_readiness, FENCING_MIN_EA
        assert FENCING_MIN_EA == "1.50"
        problems = asyncio.run(_activation_readiness(None, self._account("1.49")))
        assert len(problems) == 1
        assert "command fencing" in problems[0]

    def test_fencing_capable_ea_passes(self):
        from routes.bot_routes import _activation_readiness
        assert asyncio.run(_activation_readiness(None, self._account("1.50"))) == []
        assert asyncio.run(_activation_readiness(None, self._account("1.51"))) == []

    def test_missing_ea_version_still_blocks(self):
        from routes.bot_routes import _activation_readiness
        problems = asyncio.run(_activation_readiness(None, self._account(None)))
        assert any("EA version unknown" in p for p in problems)
