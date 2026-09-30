"""r25/r26 P2-01 — every position close goes through close_commands.request_close()
(atomic close_seq + immutable (trade_id, close_seq) ledger row). No writer may
set `close_requested: True` directly and no open position may be flipped to a
generic `pending` state. The only literal occurrences left are read queries."""
import os
import re

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LITERAL = re.compile(r'["\']close_requested["\']\s*:\s*True')
QUERY_ONLY = {"integrity.py", "trade_reconciler.py"}     # find/count filters for legacy pending-close orphans

CLOSE_WRITERS = ("protection_guard.py", "position_protector.py", "friday_flat.py", "eod_flatten.py",
                 "trade_manager.py", "portfolio/risk_manager.py", "routes/telegram_routes.py",
                 "routes/diagnostic_routes.py", "routes/trade_routes.py", "routes/bridge_routes.py",
                 "routes/panic_routes.py", "routes/nl_routes.py", "scalp/engine.py")


def _py_files():
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", "node_modules", ".venv", "static")]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def _code_lines(path):
    for i, line in enumerate(open(path, encoding="utf-8", errors="ignore").read().splitlines(), 1):
        s = line.strip()
        if s and not s.startswith("#"):
            yield i, s


def test_no_direct_close_requested_writers_anywhere():
    offenders = []
    for path in _py_files():
        rel = os.path.relpath(path, BACKEND)
        if rel == "close_commands.py":
            continue
        for i, s in _code_lines(path):
            if LITERAL.search(s) and rel not in QUERY_ONLY:
                offenders.append(f"{rel}:{i}: {s}")
    assert not offenders, "position closes must use close_commands.request_close():\n" + "\n".join(offenders)


def test_query_only_files_never_write_the_flag():
    for rel in QUERY_ONLY:
        src = open(os.path.join(BACKEND, rel)).read()
        for m in LITERAL.finditer(src):
            window = src[max(0, m.start() - 400):m.start()]
            assert "$set" not in window.rsplit("update_", 1)[-1] or "find" in window[-200:], f"{rel}: write near {m.start()}"
            assert re.search(r"(find|count_documents|find_one)\s*\(", window), f"{rel}: literal outside a read query"


def test_every_close_writer_uses_request_close():
    for rel in CLOSE_WRITERS:
        assert "request_close(" in open(os.path.join(BACKEND, rel)).read(), rel


def test_manual_close_keeps_position_open():
    src = open(os.path.join(BACKEND, "routes/trade_routes.py")).read()
    body = src[src.index('@router.post("/{trade_id}/close")'):src.index("_DELETABLE_STATUSES")]
    assert '"status": "pending"' not in body and "request_close(" in body


def test_scalp_and_reversal_use_fenced_protocol_modification():
    eng = open(os.path.join(BACKEND, "scalp/engine.py")).read()
    body = eng[eng.index("async def _request_close("):eng.index("async def _release_submission_slot_of(")]
    assert "request_close(" in body and "stamp_pending_modification" not in body
    assert 'pending_modification={"type": "FULL_CLOSE"' in body
    br = open(os.path.join(BACKEND, "routes/bridge_routes.py")).read()
    rev = br[br.index("rev = await db.trades.insert_one(rev_doc)"):br.index("inout REVERSAL on ticket")]
    assert "request_close(" in rev and 'reason="unexpected_reversal"' in rev


def test_poll_dispatches_open_close_commands_without_pending_flip():
    br = open(os.path.join(BACKEND, "routes/bridge_routes.py")).read()
    blk = br[br.index("# 1b. OPEN positions with an outstanding close command"):br.index("# 2. Open trades with pending modifications")]
    assert '"status": "open", "close_requested": {"$eq": True}' in blk
    assert '"close_idem_key": t.get("close_idem_key")' in blk and '"close_seq": t.get("close_seq", 0)' in blk
    assert '"pending_modification.type": {"$ne": "FULL_CLOSE"}' in blk      # no double dispatch


def test_close_service_is_transactional_and_fails_closed():
    src = open(os.path.join(BACKEND, "close_commands.py")).read()
    assert "class TransactionsUnavailable" in src
    assert "from nl_execution import transactions_required" in src
    ack = src[src.index("async def acknowledge_close("):src.index("async def _acknowledge_close(")]
    assert "_with_txn(" in ack
