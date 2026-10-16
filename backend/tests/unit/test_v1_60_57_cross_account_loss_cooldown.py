"""#56 — cross-account loss cooldown (16 Oct: RoboForex XAUUSD BUY hit SL −1R 15:14:55, OnEquity opened the same BUY 43 s later)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loss_cooldown import CROSS_ACCOUNT_PREFIX, is_real_loss, loss_cooldown_block, trade_r_multiple  # noqa: E402


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *a, **k):
        return self

    def limit(self, n):
        self.rows = self.rows[:n]
        return self

    async def to_list(self, length=None):
        return list(self.rows)


class _DB:
    def __init__(self, rows):
        self.rows = rows
        self.trades = self

    def find(self, q):
        out = []
        for r in self.rows:
            if r.get("status") != q["status"] or r.get("action") != q["action"] or r.get("origin") != q["origin"]:
                continue
            if not (float(r.get("pnl") or 0) < 0) or r.get("closed_at") < q["closed_at"]["$gte"] or r.get("stats_excluded") is True:
                continue
            if "account_id" in q and r.get("account_id") != q["account_id"]:
                continue
            out.append(r)
        self.last_query = q
        return _Cursor(out)


def _trade(acc, action="BUY", pnl=-25.0, r=None, mins_ago=1, symbol="XAUUSD+", **kw):
    return {"user_id": "u1", "account_id": acc, "symbol": symbol, "action": action, "status": "closed", "origin": "auto", "pnl": pnl,
            "closed_at": (datetime.now(timezone.utc) - timedelta(minutes=mins_ago)).isoformat(), "r_multiple": r, **kw}


def test_r_multiple_and_real_loss_classification():
    assert trade_r_multiple({"r_multiple": -1.0}) == -1.0
    assert trade_r_multiple({"entry_price": 4160.0, "stop_loss": 4156.0, "exit_price": 4156.0, "action": "BUY"}) == pytest.approx(-1.0)
    assert trade_r_multiple({"entry_price": 4160.0, "stop_loss": 4164.0, "exit_price": 4162.0, "action": "SELL"}) == pytest.approx(-0.5)
    assert trade_r_multiple({"entry_price": 4160.0}) is None
    assert is_real_loss({"r_multiple": -1.0}) and is_real_loss({"r_multiple": -0.11})
    assert not is_real_loss({"r_multiple": -0.05, "pnl": -0.8})            # BE / scratch close is not a loss for #56
    assert is_real_loss({"pnl": -3.0}) and not is_real_loss({"pnl": 0.0})   # R unknown → negative P&L counts (conservative)


def test_cross_account_loss_pauses_the_other_account_same_symbol_direction():
    """16 Oct replay: RoboForex loses XAUUSD BUY at −1R → OnEquity's XAUUSD BUY 43 s later is blocked."""
    db = _DB([_trade("robo", r=-1.0, symbol="XAUUSD.crp")])
    msg = _run(loss_cooldown_block(db, "u1", "XAUUSD+", "BUY", {"account_id": "onequity", "loss_cooldown_minutes": 30}))
    assert msg and msg.startswith(CROSS_ACCOUNT_PREFIX) and "ALL accounts" in msg and "-1.00R" in msg and "robo" in msg
    assert "account_id" not in db.last_query                                  # every account of the user is scanned
    # own-account loss keeps the original (per-account) message
    own = _run(loss_cooldown_block(db, "u1", "XAUUSD", "BUY", {"account_id": "robo"}))
    assert own and own.startswith("Loss cooldown:") and "this account" in own
    # other direction / other symbol / disabled / zero minutes → not blocked
    assert _run(loss_cooldown_block(db, "u1", "XAUUSD", "SELL", {"account_id": "onequity"})) is None
    assert _run(loss_cooldown_block(db, "u1", "EURUSD", "BUY", {"account_id": "onequity"})) is None
    assert _run(loss_cooldown_block(db, "u1", "XAUUSD", "BUY", {"account_id": "onequity", "loss_cooldown_enabled": False})) is None
    assert _run(loss_cooldown_block(db, "u1", "XAUUSD", "BUY", {"account_id": "onequity", "loss_cooldown_minutes": 0})) is None


def test_cross_account_rule_ignores_scratch_closes_manual_trades_and_uses_the_evaluating_accounts_duration():
    # another account's break-even close (−0.05R) must not pause this account
    db = _DB([_trade("robo", r=-0.05, pnl=-0.9)])
    assert _run(loss_cooldown_block(db, "u1", "XAUUSD", "BUY", {"account_id": "onequity"})) is None
    # … but the SAME account's small loss still counts for its own per-account cooldown (unchanged behaviour)
    assert _run(loss_cooldown_block(db, "u1", "XAUUSD", "BUY", {"account_id": "robo"})).startswith("Loss cooldown:")
    # manual losses never freeze the bot; stats-excluded late fills neither
    db = _DB([_trade("robo", r=-1.0, origin="manual"), _trade("robo", r=-1.0, stats_excluded=True)])
    assert _run(loss_cooldown_block(db, "u1", "XAUUSD", "BUY", {"account_id": "onequity"})) is None
    # duration = the evaluating account's loss_cooldown_minutes (default 30): a 20-min-old loss blocks at 30 but not at 15
    db = _DB([_trade("robo", r=-1.0, mins_ago=20)])
    assert _run(loss_cooldown_block(db, "u1", "XAUUSD", "BUY", {"account_id": "onequity"})) is not None
    assert _run(loss_cooldown_block(db, "u1", "XAUUSD", "BUY", {"account_id": "onequity", "loss_cooldown_minutes": 15})) is None
    # R derived from prices when r_multiple is missing
    db = _DB([_trade("robo", r=None, entry_price=4160.0, stop_loss=4156.0, exit_price=4156.0)])
    assert "-1.00R" in _run(loss_cooldown_block(db, "u1", "XAUUSD", "BUY", {"account_id": "onequity"}))


def test_bot_runner_pulses_cross_account_loss_cooldown_and_counts_it():
    br = open(os.path.join(BACKEND, "bot_runner.py")).read()
    assert "CROSS_ACCOUNT_PREFIX" in br and '"cross_account_loss_cooldown_block" if cross else "loss_cooldown_block"' in br
    i = br.index("lc = await loss_cooldown_block(")
    assert "_record_pulse(" in br[i:i + 600] and 'action="SKIP"' in br[i:i + 600]
    from models import BotConfigUpdate
    cfg = BotConfigUpdate().model_dump()
    assert cfg["loss_cooldown_enabled"] is True and cfg["loss_cooldown_minutes"] == 30
