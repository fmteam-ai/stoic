"""ExecutionAgent — thin wrapper around the engine-per-account broker layer.

The Orchestrator should never call execution directly; it goes through the
ExecutionAgent so we can centralise:
  - choice of target account (default vs first-connected)
  - emit a uniform activity log entry on success/failure

The actual broker calls remain in `execution.py` (paper engine + MT5 bridge
queue). This wrapper does NOT change order-routing logic.
"""
import logging

from execution import for_account as engine_for_account

logger = logging.getLogger("agent.execution")


class ExecutionAgent:
    name = "execution"

    async def execute(self, user_id: str, account: dict, signal: dict) -> dict | None:
        if not signal or signal.get("action") not in ("BUY", "SELL"):
            return None
        engine = engine_for_account(account)
        trade = await engine.execute(
            user_id=user_id,
            account=account,
            signal={
                "signal_id": signal.get("signal_id"),
                "symbol": signal["symbol"],
                "action": signal["action"],
                "lot_size": signal["lot_size"],
                "entry_price": signal["entry_price"],
                "stop_loss": signal["stop_loss"],
                "take_profit": signal["take_profit"],
                "origin": signal.get("origin", "auto"),
            },
        )
        return trade
