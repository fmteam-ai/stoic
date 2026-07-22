"""iter-146 · Audit r5 — EA exact-once execution corrections:
success-only intent consumption, journaled replay outcomes, fenced
close_requested path, superseded/terminal/retryable acks, volume steps,
stop tolerance, OrderCheck on retry."""

import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
EA = _os.path.join(_BACKEND_DIR, "static/EmergentTradingBridge.mq5")
BR = _os.path.join(_BACKEND_DIR, "routes/bridge_routes.py")


def _ea():
    with open(EA) as f:
        return f.read()


def _br():
    with open(BR) as f:
        return f.read()


def _fn(src, name, nxt):
    i = src.index(name)
    j = src.index(nxt, i)
    return src[i:j]


class TestSuccessOnlyIntentConsumption:                       # audit r5 item 1
    def test_modify_sl_marks_only_on_success(self):
        body = _fn(_ea(), "void ApplyModifySL(", "void ApplyPartialClose(")
        # v1.52 — intent consumption additionally requires the live position
        # to SHOW the stop (accepted != confirmed)
        assert "if (success && stop_confirmed) MarkIntentDone(intent, seq, trade_id);" in body
        assert body.count("MarkIntentDone(") == 1

    def test_partial_close_marks_only_on_success(self):
        body = _ea()[_ea().index("void ApplyPartialClose("):]
        assert "if (success) MarkIntentDone(intent, seq, trade_id);" in body
        # the below-min terminal branch consumes with outcome 2
        assert "MarkIntentDone(intent, seq, trade_id, 2);" in body

    def test_full_close_marks_only_on_verified_success(self):
        body = _fn(_ea(), "void ApplyFullClose(", "// v1.50 — server 'close_requested'")
        assert "if (success) MarkIntentDone(intent, seq, trade_id);" in body
        # success is the ABSENCE of the broker position, not the retcode
        assert "bool success = absent;" in body
        assert "partial_close_remaining" in body

    def test_mark_intent_done_journals_outcome(self):
        body = _fn(_ea(), "void MarkIntentDone(", "// Drop journal entries")
        assert "double outcome = 1" in body
        assert "GlobalVariableSet(JKey(\"I\", intent), outcome);" in body


class TestJournaledReplay:                                    # audit r5 item 2
    def test_replay_uses_journaled_outcome_and_live_facts(self):
        body = _fn(_ea(), "void ParseModificationsBlock(", "string ExtractString(")
        assert 'GlobalVariableGet(JKey("I", intent))' in body
        assert "bool osucc = (outc != 2);" in body
        assert "confirmed_position_sl" in body      # live fact on replay
        assert "remaining_volume" in body
        assert '\\"terminal\\":true,' in body       # failed outcome replays as terminal


class TestFencedClosePosition:                                # audit r5 item 3
    def _body(self):
        return _fn(_ea(), "void ClosePosition(", "// ----- v1.10: SL/TP modify")

    def test_deterministic_close_intent(self):
        body = self._body()
        assert 'string intent = "close-" + trade_id;' in body
        assert "IntentDone(intent)" in body
        assert "MarkIntentDone(intent, 0, trade_id);" in body

    def test_closed_only_when_position_verified_absent(self):
        body = self._body()
        i_send = body.index("OrderSend(req, res)")
        seg = body[i_send:]
        assert "if (PositionSelectByTicket(ticket))" in seg   # re-check
        assert "return;" in seg
        # journaled result for replay
        assert 'JSet("X", trade_id, res.price);' in seg
        assert 'JSet("L", trade_id, pnl);' in seg

    def test_replay_reports_journaled_close(self):
        body = self._body()
        assert '\\"replay\\":true' in body
        assert 'JGet("X", trade_id)' in body and 'JGet("L", trade_id)' in body

    def test_pnl_captured_before_close(self):
        body = self._body()
        assert body.index("PositionGetDouble(POSITION_PROFIT)") \
            < body.index("OrderSend(req, res)")


class TestSupersededAck:                                      # audit r5 item 4
    def test_ea_sends_structured_superseded_ack(self):
        body = _fn(_ea(), "void ParseModificationsBlock(", "string ExtractString(")
        assert '\\"superseded\\":true' in body
        assert "stale_sequence" in body
        assert "received_seq" in body and "latest_seq" in body

    def test_backend_clears_without_marking_executed(self):
        src = _br()
        i = src.index("if payload.superseded:")
        seg = src[i:i + 700]
        assert '"pending_modification": None' in seg
        assert "superseded_cleared" in seg
        assert "executed_intents" not in seg


class TestMissingPositionAcks:                                # audit r5 item 5
    def test_helper_distinguishes_terminal_vs_retryable(self):
        body = _fn(_ea(), "void AckMissingPosition(", "void OnTimer()")
        assert "PositionClosedInHistory(ticket)" in body
        assert "position_not_found" in body
        assert "position_temporarily_unavailable" in body
        assert '\\"terminal\\":true,' in body and '\\"retryable\\":true,' in body
        assert "MarkIntentDone(intent, 0, trade_id, 2);" in body

    def test_modify_and_partial_use_helper(self):
        src = _ea()
        for fn, nxt in (("void ApplyModifySL(", "void ApplyPartialClose("),):
            assert 'AckMissingPosition(trade_id, "MODIFY_SL"' in _fn(src, fn, nxt)
        assert 'AckMissingPosition(trade_id, "PARTIAL_CLOSE"' in \
            src[src.index("void ApplyPartialClose("):]

    def test_backend_keeps_pending_on_retryable(self):
        src = _br()
        i = src.index("if payload.retryable and not payload.success:")
        seg = src[i:i + 600]
        assert "retry_pending" in seg
        assert "retry_count" in seg
        assert '"pending_modification": None' not in seg   # NOT cleared


class TestComboSplit:                                         # audit r5 item 6
    def test_ea_no_longer_chains_combo_modify(self):
        body = _fn(_ea(), "void ParseModificationsBlock(", "string ExtractString(")
        assert 'ApplyModifySL(trade_id, ticket, new_sl, "", 0)' not in body

    def test_backend_enqueues_fenced_followup(self):
        src = _br()
        assert '"reason": "tier1_combo_followup"' in src
        i = src.index("tier1_combo_followup")
        seg = src[i - 600:i + 200]
        assert "stamp_pending_modification" in seg
        # direct combo apply removed from the PC success branch
        assert 'update["stop_loss"] = float(mod_new_sl)' not in src


class TestStopToleranceAndVolumeSteps:                        # items 8 + 9
    def test_stop_confirmed_tick_tolerance(self):
        body = _fn(_ea(), "void ApplyModifySL(", "void ApplyPartialClose(")
        assert "SYMBOL_TRADE_TICK_SIZE" in body
        assert "MathAbs(confirmed_sl - adj_sl) <= tick_sz" in body
        assert '\\"stop_confirmed\\":%s' in body

    def test_backend_stores_stop_confirmed(self):
        assert 'update["stop_confirmed"] = bool(payload.stop_confirmed)' in _br()

    def test_partial_close_uses_broker_volume_steps(self):
        body = _ea()[_ea().index("void ApplyPartialClose("):]
        assert "SYMBOL_VOLUME_STEP" in body
        assert "SYMBOL_VOLUME_MIN" in body
        assert "MathFloor(close_vol / step + 0.5) * step" in body
        assert "close_volume_below_min" in body
        assert "NormalizeDouble(close_vol, 2)" not in body


class TestOrderCheckOnRetry:                                  # item 10
    def test_retry_reruns_preflight(self):
        body = _fn(_ea(), "void ExecuteTrade(", "// ----- v1.40: FULL_CLOSE")
        assert body.count("OrderCheck(req, chk)") == 2
        assert "preflight_failed_retry" in body


class TestBackendAckModelRound5:
    def test_new_fields(self):
        import sys
        sys.path.insert(0, _BACKEND_DIR)
        from routes.bridge_routes import BridgeModificationAck
        f = BridgeModificationAck.model_fields
        for k in ("superseded", "received_seq", "latest_seq",
                  "terminal", "retryable", "stop_confirmed"):
            assert k in f, k

    def test_terminal_error_recorded(self):
        assert 'update["mod_terminal_error"] = payload.error' in _br()
