"""r25 P2-01 follow-up — every position-close writer goes through
close_commands.request_close (atomic close_seq + immutable ledger row).
Direct `"close_requested": True` $set writes on OPEN trades are refused at
source level; the remaining literal sites are queries or a different verb."""
import os
import re

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# path → why a literal `"close_requested": True` is still allowed there
ALLOWED = {
    "close_commands.py": "the protocol itself",
    "nl_execution.py": "close_command_update: atomic seq helper (same protocol semantics)",
    "integrity.py": "queries only (status=pending + close_requested)",
    "trade_reconciler.py": "queries only",
    "routes/trade_routes.py": "user × CLOSE on a PENDING ticketed order — not an open-position close",
    "routes/bridge_routes.py": "insert_one of a reversal-open doc flagged for immediate flatten",
    "portfolio/risk_manager.py": "module docstring only",
}

MIGRATED = ("protection_guard.py", "position_protector.py", "friday_flat.py", "eod_flatten.py",
            "trade_manager.py", "portfolio/risk_manager.py", "routes/telegram_routes.py",
            "routes/diagnostic_routes.py")


def _py_files():
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", "node_modules", ".venv", "static")]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def test_no_unlisted_direct_close_requested_writers():
    offenders = []
    for path in _py_files():
        rel = os.path.relpath(path, BACKEND)
        if rel in ALLOWED:
            continue
        src = open(path, encoding="utf-8", errors="ignore").read()
        for i, line in enumerate(src.splitlines(), 1):
            s = line.strip()
            if s.startswith("#"):
                continue
            if re.search(r'["\']close_requested["\']\s*:\s*True', s):
                offenders.append(f"{rel}:{i}: {s}")
    assert not offenders, "direct close writers must use close_commands.request_close():\n" + "\n".join(offenders)


def test_migrated_modules_call_request_close():
    for rel in MIGRATED:
        assert "request_close(" in open(os.path.join(BACKEND, rel)).read(), rel
