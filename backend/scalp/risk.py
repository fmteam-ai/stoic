"""Scalp subsystem · Steps 11/12 — deterministic local risk checks + sizing.

Fail-closed everywhere: any state we cannot compute means NO trade.
"""
import math
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class ScalpRiskLimits:
    risk_fraction: float = 0.0005          # 0.05% equity per scalp
    max_open_positions: int = 1
    max_trades_per_symbol_per_hour: int = 20
    max_consecutive_losses: int = 4
    cooldown_after_loss_streak_minutes: int = 30
    max_daily_loss_pct: float = 0.5        # scalp-scope daily loss stop
    max_daily_cost_pct: float = 0.35
    max_holding_ms: int = 300_000          # 5 min hard timeout


DEFAULT_LIMITS = ScalpRiskLimits()


class RiskState:
    """Per-(account, symbol) rolling risk counters (in-memory, mirrored async)."""

    def __init__(self, limits: ScalpRiskLimits = DEFAULT_LIMITS):
        self.limits = limits
        self.trade_times: deque = deque(maxlen=200)   # epoch seconds
        self.consecutive_losses = 0
        self.cooldown_until = 0.0
        self.daily_loss_usd = 0.0
        self.daily_cost_usd = 0.0
        self.daily_key = ""
        self.open_scalps = 0

    def _roll_day(self):
        key = time.strftime("%Y-%m-%d", time.gmtime())
        if key != self.daily_key:
            self.daily_key = key
            self.daily_loss_usd = 0.0
            self.daily_cost_usd = 0.0

    def record_result(self, net_pnl_usd: float, cost_usd: float):
        self._roll_day()
        self.daily_cost_usd += abs(cost_usd)
        if net_pnl_usd < 0:
            self.daily_loss_usd += -net_pnl_usd
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.limits.max_consecutive_losses:
                self.cooldown_until = time.time() + \
                    self.limits.cooldown_after_loss_streak_minutes * 60
        else:
            self.consecutive_losses = 0

    def record_open(self):
        self.trade_times.append(time.time())
        self.open_scalps += 1

    def record_close(self):
        self.open_scalps = max(0, self.open_scalps - 1)

    # ---- persistence (review item 9): survive restarts ----
    def to_doc(self) -> dict:
        return {
            "trade_times": list(self.trade_times),
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": self.cooldown_until,
            "daily_loss_usd": self.daily_loss_usd,
            "daily_cost_usd": self.daily_cost_usd,
            "daily_key": self.daily_key,
        }

    def load_doc(self, doc: dict) -> None:
        self.trade_times = deque(
            [float(t) for t in doc.get("trade_times") or []], maxlen=200)
        self.consecutive_losses = int(doc.get("consecutive_losses") or 0)
        self.cooldown_until = float(doc.get("cooldown_until") or 0.0)
        key = time.strftime("%Y-%m-%d", time.gmtime())
        if doc.get("daily_key") == key:      # same UTC day → restore budgets
            self.daily_key = key
            self.daily_loss_usd = float(doc.get("daily_loss_usd") or 0.0)
            self.daily_cost_usd = float(doc.get("daily_cost_usd") or 0.0)


def size_lot(equity: float, stop_pips: float, pip_value_per_lot: float,
             cfg, limits: ScalpRiskLimits) -> dict:
    """Step 11 sizing — reject when the broker minimum exceeds the budget."""
    if equity <= 0 or stop_pips <= 0 or pip_value_per_lot <= 0:
        return {"lot": 0.0, "ok": False, "reason": "sizing inputs invalid — fail closed"}
    risk_budget = equity * limits.risk_fraction
    raw = risk_budget / (stop_pips * pip_value_per_lot)
    lot = math.floor(raw / cfg.lot_step) * cfg.lot_step
    lot = round(lot, 2)
    if lot < cfg.min_lot:
        lot = cfg.min_lot
    actual_risk = lot * stop_pips * pip_value_per_lot
    if actual_risk > risk_budget * 1.05:
        return {"lot": 0.0, "ok": False,
                "reason": (f"minimum tradable volume risks ${actual_risk:.2f} "
                           f"vs ${risk_budget:.2f} budget — rejected"),
                "risk_budget_usd": round(risk_budget, 2)}
    return {"lot": lot, "ok": True, "reason": None,
            "risk_budget_usd": round(risk_budget, 2),
            "actual_risk_usd": round(actual_risk, 2)}


def check(rs: RiskState, equity: float, stop_pips: float,
          pip_value_per_lot: float, cfg) -> dict:
    """All Step 12 guards; returns {ok, reason, lot}."""
    rs._roll_day()
    L = rs.limits
    now = time.time()
    if now < rs.cooldown_until:
        return {"ok": False, "lot": 0.0,
                "reason": f"loss-streak cooldown ({int(rs.cooldown_until - now)}s left)"}
    if rs.open_scalps >= L.max_open_positions:
        return {"ok": False, "lot": 0.0, "reason": "max open scalp positions"}
    hour_ago = now - 3600
    if sum(1 for t in rs.trade_times if t >= hour_ago) >= L.max_trades_per_symbol_per_hour:
        return {"ok": False, "lot": 0.0, "reason": "hourly trade cap"}
    if equity > 0:
        if rs.daily_loss_usd >= equity * L.max_daily_loss_pct / 100.0:
            return {"ok": False, "lot": 0.0, "reason": "daily scalp loss limit"}
        if rs.daily_cost_usd >= equity * L.max_daily_cost_pct / 100.0:
            return {"ok": False, "lot": 0.0, "reason": "daily cost budget exhausted"}
    sized = size_lot(equity, stop_pips, pip_value_per_lot, cfg, L)
    if not sized["ok"]:
        return {"ok": False, "lot": 0.0, "reason": sized["reason"]}
    return {"ok": True, "lot": sized["lot"], "reason": None,
            "risk_budget_usd": sized.get("risk_budget_usd"),
            "actual_risk_usd": sized.get("actual_risk_usd")}
