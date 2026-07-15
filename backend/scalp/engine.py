"""Scalp subsystem · fast-path orchestrator.

Pipeline (spec final recommendation):
  regime permission → microstructure setup → probability → expected move
  → execution cost → conservative net expectancy → risk approval
  → final fresh-quote check → order → reconciliation → attribution

Hot-path rules: no LLM, no news downloads, no blocking DB reads between the
incoming tick and the order decision. Mongo writes are fire-and-forget
asyncio tasks; permissions refresh on a slow async loop.

Shadow labeling: EVERY candidate that reaches the forecast stage gets a
barrier simulation (entry at executable side + expected slippage), so the
training set is unbiased across the score distribution — not only trades
that passed every gate.
"""
import asyncio
import logging
from datetime import datetime, timezone

from scalp import edge, gate, kill, permissions, setup
from scalp import model as scalp_model
from scalp.costs import dynamic_spread_limit
from scalp.features import snapshot
from scalp.forecast import make as make_forecast
from scalp.instruments import approved
from scalp.risk import DEFAULT_LIMITS, RiskState, check as risk_check
from scalp.state import ScalpState, TickEvent, now_ms

logger = logging.getLogger("scalp.engine")

EVAL_THROTTLE_MS = 1000
TICK_FLUSH_N = 200
TICK_FLUSH_MS = 10_000
RETRAIN_EVERY_RESOLUTIONS = 200

_runners: dict = {}


class ShadowSim:
    """Barrier-label tracker (Step 7): target vs stop on executable side."""

    def __init__(self, decision_id, direction, entry_px, target_pips, stop_pips,
                 pip, opened_ms, max_holding_ms):
        self.decision_id = decision_id
        self.direction = direction
        self.entry_px = entry_px
        self.pip = pip
        self.opened_ms = opened_ms
        self.deadline_ms = opened_ms + max_holding_ms
        if direction == "BUY":
            self.target_px = entry_px + target_pips * pip
            self.stop_px = entry_px - stop_pips * pip
        else:
            self.target_px = entry_px - target_pips * pip
            self.stop_px = entry_px + stop_pips * pip

    def advance(self, t: TickEvent):
        """Long exits at BID, short exits at ASK. Returns outcome dict or None."""
        px = t.bid if self.direction == "BUY" else t.ask
        if self.direction == "BUY":
            if px <= self.stop_px:
                return self._done("stop_first", px, t)
            if px >= self.target_px:
                return self._done("target_first", px, t)
        else:
            if px >= self.stop_px:
                return self._done("stop_first", px, t)
            if px <= self.target_px:
                return self._done("target_first", px, t)
        if t.broker_time_ms >= self.deadline_ms:
            return self._done("timeout", px, t)
        return None

    def _done(self, result, px, t):
        signed = (px - self.entry_px) if self.direction == "BUY" else (self.entry_px - px)
        return {"result": result,
                "net_pips": round(signed / self.pip, 2),
                "time_to_exit_ms": int(t.broker_time_ms - self.opened_ms),
                "exit_px": px,
                "resolved_at": datetime.now(timezone.utc).isoformat()}


class ScalpRunner:
    def __init__(self, account_id: str, user_id: str, symbol: str):
        self.account_id = account_id
        self.user_id = user_id
        self.symbol = symbol
        self.cfg = approved(symbol)
        self.state = ScalpState(self.cfg.pip_size)
        self.risk_state = RiskState(DEFAULT_LIMITS)
        self.equity = 0.0
        self.mode = "shadow"                # shadow | demo_live
        self.enabled = False
        self.health = {"status": "OK", "open_allowed": False, "reasons": []}
        self.open_sims: list[ShadowSim] = []
        self.live_trades: dict = {}          # trade_id -> {opened_ms, direction, ...}
        self._last_eval_ms = 0
        self._tick_buffer: list = []
        self._last_flush_ms = now_ms()
        self._resolutions_since_retrain = 0
        self.counters = {"ticks": 0, "evals": 0, "candidates": 0,
                         "shadow_trades": 0, "live_trades": 0,
                         "rejected": 0}
        self.last_decision: dict | None = None

    # ---------------- ingestion ----------------

    async def ingest(self, db, account: dict, ticks: list[dict],
                     sent_at_ms: int | None) -> dict:
        self.equity = float(account.get("equity") or 0)
        recv = now_ms()
        for raw in ticks:
            try:
                t = TickEvent(symbol=self.symbol,
                              broker_time_ms=int(raw["tm"]),
                              received_time_ms=recv,
                              bid=float(raw["b"]), ask=float(raw["a"]))
            except (KeyError, TypeError, ValueError):
                continue
            self.state.update(t)
            self.counters["ticks"] += 1
            self._advance_sims(db, t)
            self._monitor_live_exits(db, t)
            self._tick_buffer.append({"tm": t.broker_time_ms, "b": t.bid, "a": t.ask})
        self.health = kill.evaluate(self.state, self.cfg)
        permissions.maybe_refresh(db, self.user_id, self.symbol, self.cfg)
        self._maybe_flush_ticks(db)
        if self.enabled:
            await self._maybe_evaluate(db)
        return {"ok": True, "ticks": len(ticks),
                "health": self.health["status"], "enabled": self.enabled}

    # ---------------- decision pipeline ----------------

    async def _maybe_evaluate(self, db):
        nm = now_ms()
        if nm - self._last_eval_ms < EVAL_THROTTLE_MS:
            return
        self._last_eval_ms = nm
        self.counters["evals"] += 1

        feats = snapshot(self.state)
        if feats is None:
            return
        perms = permissions.get_cached(self.user_id, self.symbol)
        cand = setup.detect(feats, self.state)
        if cand is None:
            return
        self.counters["candidates"] += 1
        direction = cand["direction"]
        perm_ok = (perms.get("long_enabled") if direction == "BUY"
                   else perms.get("short_enabled"))

        model_p = scalp_model.predict_p(self.symbol, feats)
        fc = make_forecast(feats, cand, self.state, self.cfg, model_p=model_p)
        edge_res = edge.evaluate(fc)

        from pip_utils import pip_value_usd_per_lot
        pip_val = pip_value_usd_per_lot(self.symbol, None)
        risk_res = risk_check(self.risk_state, self.equity, fc.stop_pips,
                              pip_val, self.cfg)
        spread_limit = dynamic_spread_limit(self.state, self.cfg)
        gate_res = gate.final_execution_gate(
            self.state, self.cfg, edge_res["net_edge_pips"],
            signal_ts_ms=nm, spread_limit_pips=spread_limit,
            health_open_allowed=self.health["open_allowed"])

        all_ok = bool(perm_ok) and edge_res["ok"] and risk_res["ok"] and gate_res["ok"]
        if all_ok and self.mode == "demo_live":
            verdict = "live_traded"
        elif all_ok:
            verdict = "shadow_traded"
        else:
            verdict = "rejected"
            self.counters["rejected"] += 1

        t = self.state.last_tick
        entry_px = ((t.ask + fc.expected_slippage_pips * self.cfg.pip_size)
                    if direction == "BUY"
                    else (t.bid - fc.expected_slippage_pips * self.cfg.pip_size))

        doc = {
            "user_id": self.user_id, "account_id": self.account_id,
            "symbol": self.symbol, "ts_ms": nm,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "direction": direction, "mode": self.mode,
            "features": {k: v for k, v in feats.items() if not k.startswith("_")},
            "setup": cand, "forecast": fc.to_dict(),
            "net_edge_pips": edge_res["net_edge_pips"],
            "cost_pips": edge_res["cost_pips"],
            "cost_ratio": edge_res["cost_ratio"],
            "gates": {
                "permission": {"ok": bool(perm_ok), "regime": perms.get("regime"),
                               "reasons": perms.get("reasons", [])},
                "edge": edge_res, "risk": risk_res, "final": gate_res,
            },
            "verdict": verdict,
            "lot": risk_res.get("lot", 0.0),
            "sim": {"entry_px": entry_px,
                    "target_pips": fc.target_pips, "stop_pips": fc.stop_pips},
            "outcome": None,
        }
        res = await db.scalp_decisions.insert_one(doc)
        self.last_decision = {**doc, "_id": str(res.inserted_id)}

        # barrier sim for EVERY forecast-stage candidate (unbiased labels)
        self.open_sims.append(ShadowSim(
            res.inserted_id, direction, entry_px, fc.target_pips, fc.stop_pips,
            self.cfg.pip_size, t.broker_time_ms,
            self.risk_state.limits.max_holding_ms))
        if verdict == "shadow_traded":
            self.counters["shadow_trades"] += 1
        if verdict == "live_traded":
            await self._submit_live(db, doc, fc, risk_res)

    async def _submit_live(self, db, decision: dict, fc, risk_res):
        """Order submission via the existing MT5 bridge queue (broker-visible SL/TP)."""
        t = self.state.last_tick
        pip = self.cfg.pip_size
        entry = t.ask if decision["direction"] == "BUY" else t.bid
        if decision["direction"] == "BUY":
            sl, tp = entry - fc.stop_pips * pip, entry + fc.target_pips * pip
        else:
            sl, tp = entry + fc.stop_pips * pip, entry - fc.target_pips * pip
        from execution import engine_for_account
        account = await db.accounts.find_one({"_id": _oid(self.account_id)})
        if not account:
            return
        engine = engine_for_account(account)
        trade = await engine.execute(
            user_id=self.user_id, account=account,
            signal={"symbol": self.symbol, "action": decision["direction"],
                    "lot_size": risk_res["lot"],
                    "entry_price": round(entry, 5),
                    "stop_loss": round(sl, 5), "take_profit": round(tp, 5),
                    "origin": "auto", "scope": "scalp_fast",
                    "scalp_decision_id": str(decision.get("_id") or "")},
            cfg_account_id=self.account_id)
        if trade.get("blocked"):
            self.state.record_reject()
            return
        tid = str(trade.get("id") or trade.get("_id") or "")
        self.risk_state.record_open()
        self.counters["live_trades"] += 1
        self.live_trades[tid] = {
            "opened_ms": now_ms(), "direction": decision["direction"],
            "entry_px": entry, "stop_px": sl, "target_px": tp,
        }

    # ---------------- exits & sims ----------------

    def _advance_sims(self, db, t: TickEvent):
        still = []
        for sim in self.open_sims:
            out = sim.advance(t)
            if out is None:
                still.append(sim)
                continue
            self._resolutions_since_retrain += 1
            _bg(db.scalp_decisions.update_one(
                {"_id": sim.decision_id}, {"$set": {"outcome": out}}))
        self.open_sims = still
        if self._resolutions_since_retrain >= RETRAIN_EVERY_RESOLUTIONS:
            self._resolutions_since_retrain = 0
            _bg(scalp_model.retrain(db, self.symbol))

    def _monitor_live_exits(self, db, t: TickEvent):
        """Step 8 fast exits — broker keeps the hard SL/TP; we close EARLIER on
        timeout / spread shock / degraded health via the FULL_CLOSE queue."""
        if not self.live_trades:
            return
        nm = now_ms()
        sp = self.state.spread_pips() or 0.0
        for tid, info in list(self.live_trades.items()):
            reason = None
            if nm - info["opened_ms"] >= self.risk_state.limits.max_holding_ms:
                reason = "max_holding_time"
            elif sp >= 3.0 * self.cfg.max_spread_pips:
                reason = "spread_shock"
            elif self.health["status"] != "OK" and not self.health["open_allowed"]:
                reason = "execution_degraded"
            if reason:
                _bg(self._request_close(db, tid, reason))
                self.live_trades.pop(tid, None)

    async def _request_close(self, db, trade_id: str, reason: str):
        await db.trades.update_one(
            {"_id": _oid(trade_id), "status": "open"},
            {"$set": {"pending_modification": {
                "type": "FULL_CLOSE",
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "reason": f"scalp_{reason}"},
                "close_reason": f"scalp_{reason}"}})

    def on_trade_closed(self, trade_id: str, pnl: float):
        """Fill reconciliation hook (called from bridge report path)."""
        self.live_trades.pop(trade_id, None)
        self.risk_state.record_close()
        self.risk_state.record_result(pnl, 0.0)

    # ---------------- tick recording (Step 2, off hot path) ----------------

    def _maybe_flush_ticks(self, db):
        nm = now_ms()
        if (len(self._tick_buffer) >= TICK_FLUSH_N
                or (self._tick_buffer and nm - self._last_flush_ms >= TICK_FLUSH_MS)):
            batch, self._tick_buffer = self._tick_buffer, []
            self._last_flush_ms = nm
            _bg(db.scalp_ticks.insert_one({
                "user_id": self.user_id, "account_id": self.account_id,
                "symbol": self.symbol, "n": len(batch),
                "first_ms": batch[0]["tm"], "last_ms": batch[-1]["tm"],
                "stored_at": datetime.now(timezone.utc).isoformat(),
                "ticks": batch}))

    def status(self) -> dict:
        feats = snapshot(self.state)
        return {
            "account_id": self.account_id, "symbol": self.symbol,
            "enabled": self.enabled, "mode": self.mode,
            "health": self.health,
            "permissions": permissions.get_cached(self.user_id, self.symbol),
            "counters": self.counters,
            "open_sims": len(self.open_sims),
            "open_live": len(self.live_trades),
            "equity": self.equity,
            "spread_pips": self.state.spread_pips(),
            "quote_age_ms": self.state.quote_age_ms(),
            "features": ({k: round(v, 3) for k, v in feats.items()
                          if not k.startswith("_")} if feats else None),
            "risk": {
                "consecutive_losses": self.risk_state.consecutive_losses,
                "daily_loss_usd": round(self.risk_state.daily_loss_usd, 2),
                "open_scalps": self.risk_state.open_scalps,
            },
            "last_decision": ({k: v for k, v in self.last_decision.items()
                               if k != "features"} if self.last_decision else None),
        }


def _oid(s):
    from bson import ObjectId
    try:
        return ObjectId(s)
    except Exception:
        return s


def _bg(coro):
    try:
        asyncio.get_running_loop().create_task(_coro_safe(coro))
    except RuntimeError:
        pass


async def _coro_safe(coro):
    try:
        await coro
    except Exception as e:  # noqa: BLE001
        logger.debug("scalp bg task failed: %s", e)


def get_runner(account_id: str, user_id: str, symbol: str) -> ScalpRunner | None:
    if approved(symbol) is None:
        return None
    key = f"{account_id}:{symbol.upper()}"
    r = _runners.get(key)
    if r is None:
        r = ScalpRunner(account_id, user_id, symbol.upper())
        _runners[key] = r
    return r


def runners_for_account(account_id: str) -> list:
    return [r for k, r in _runners.items() if k.startswith(f"{account_id}:")]


async def apply_config(db, account: dict, symbol: str, enabled: bool, mode: str):
    r = get_runner(str(account["_id"]), account["user_id"], symbol)
    if r is None:
        return None
    r.enabled = enabled
    r.mode = mode
    await scalp_model.load_persisted(db, symbol.upper())
    await db.scalp_configs.update_one(
        {"account_id": str(account["_id"]), "symbol": symbol.upper()},
        {"$set": {"user_id": account["user_id"], "enabled": enabled, "mode": mode,
                  "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True)
    return r
